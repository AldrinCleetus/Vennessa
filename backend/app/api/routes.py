from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from app.core.analyze import analyze, estimate_load
from app.core.compare import RANK, comparison_markdown
from app.core.compare import compare as compare_architectures
from app.core.engine import Simulation, run as run_sim
from app.core.model import Architecture, load_architecture
from app.core.patch import (
    PatchConflictError,
    PatchValidationError,
    apply_architecture_patch,
    architecture_fingerprint,
    diff_architectures,
)
from app.core.scenario import Scenario, load_scenario
from app.core.verification import failure_report, verify_fixes, verification_reason
from app.ai.parser import generate_architecture, text_to_architecture_yaml
from app.ai.adversary import generate_adversary_scenarios
from app.ai.remediator import generate_remediation_fixes, remediate_architecture

router = APIRouter()
MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "scenarios"


class SimulateRequest(BaseModel):
    architecture: Dict[str, Any] | str  # raw dict or model filename
    scenario: Dict[str, Any] | str      # raw dict or scenario filename
    seed: Optional[int] = None


class AnalyzeRequest(BaseModel):
    architecture: Dict[str, Any] | str


class TextToModelRequest(BaseModel):
    description: str
    name: Optional[str] = None
    mode: Optional[Literal["create", "modify"]] = None
    current_architecture: Optional[Dict[str, Any] | str] = None


class AdversaryRequest(BaseModel):
    architecture: Dict[str, Any] | str
    count: int = 3
    seed: Optional[int] = None
    exclude_ids: List[str] = Field(default_factory=list)
    include_analysis: bool = True


class RemediateRequest(BaseModel):
    architecture: Dict[str, Any] | str
    simulation_result: Dict[str, Any]
    findings: List[Dict[str, Any]] = Field(default_factory=list)
    count: int = 3
    architecture_fingerprint: Optional[str] = None
    # Additive: required to verify fixes by re-simulating the same stress test.
    scenario: Optional[Dict[str, Any]] = None
    rounds: int = 3


class PatchRequest(BaseModel):
    architecture: Dict[str, Any] | str
    patch: Dict[str, Any]
    fix_ids: List[str] = Field(default_factory=list)


class CompareRequest(BaseModel):
    before: Dict[str, Any] | str
    after: Dict[str, Any] | str
    scenarios: List[Dict[str, Any] | str]
    seed: Optional[int] = None


def _resolve_architecture(arch_input: Dict[str, Any] | str) -> Architecture:
    """Helper to resolve architecture from dict or model filename with clear validation error handling."""
    try:
        if isinstance(arch_input, str):
            path = MODELS_DIR / f"{arch_input}.yaml"
            if not path.exists():
                path = MODELS_DIR / arch_input
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"Architecture '{arch_input}' not found")
            return load_architecture(path)
        return Architecture.model_validate(arch_input)
    except HTTPException:
        raise
    except ValidationError as ve:
        # Extract friendly error messages from Pydantic
        error_msgs = []
        for err in ve.errors():
            loc = " -> ".join(str(l) for l in err.get("loc", []))
            msg = err.get("msg", "")
            error_msgs.append(f"{loc}: {msg}" if loc else msg)
        clean_detail = " | ".join(error_msgs) if error_msgs else str(ve)
        raise HTTPException(status_code=400, detail=f"Architecture Validation Error: {clean_detail}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid architecture specification: {str(e)}")


def _resolve_scenario(
    scenario_input: Dict[str, Any] | str, seed: int | None = None
) -> Scenario:
    """Resolve a scenario from a raw dict or a filename under scenarios/."""
    if isinstance(scenario_input, str):
        path = SCENARIOS_DIR / f"{scenario_input}.yaml"
        if not path.exists():
            path = SCENARIOS_DIR / scenario_input
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Scenario '{scenario_input}' not found")
        sc = load_scenario(path)
    else:
        sc = Scenario.model_validate(scenario_input)
    if seed is not None:
        sc.seed = seed
    return sc


def _finding_dicts(arch: Architecture) -> List[Dict[str, Any]]:
    return [
        {
            "rule": f.rule,
            "severity": f.severity,
            "target": f.target,
            "message": f.message,
            "fix": f.fix,
        }
        for f in analyze(arch)
    ]


@router.get("/models")
def list_models():
    """List all available architecture templates."""
    models = []
    if MODELS_DIR.exists():
        for p in sorted(MODELS_DIR.glob("*.yaml")):
            try:
                arch = load_architecture(p)
                models.append({
                    "id": p.stem,
                    "name": arch.name,
                    "nodes": len(arch.nodes),
                    "edges": len(arch.edges),
                    "path": str(p.name),
                })
            except Exception:
                models.append({"id": p.stem, "name": p.stem, "path": str(p.name)})
    return {"models": models}


@router.get("/models/{model_id}")
def get_model(model_id: str):
    """Retrieve an architecture model definition."""
    path = MODELS_DIR / f"{model_id}.yaml"
    if not path.exists():
        path = MODELS_DIR / model_id
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    data = yaml.safe_load(path.read_text())
    return {"id": model_id, "data": data}


@router.get("/scenarios")
def list_scenarios():
    """List available failure scenarios."""
    scenarios = []
    if SCENARIOS_DIR.exists():
        for p in sorted(SCENARIOS_DIR.glob("*.yaml")):
            try:
                sc = load_scenario(p)
                scenarios.append({
                    "id": p.stem,
                    "name": sc.name,
                    "description": sc.description,
                    "duration_s": sc.duration_s,
                    "path": str(p.name),
                })
            except Exception:
                scenarios.append({"id": p.stem, "name": p.stem, "path": str(p.name)})
    return {"scenarios": scenarios}


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: str):
    """Retrieve a failure scenario definition."""
    path = SCENARIOS_DIR / f"{scenario_id}.yaml"
    if not path.exists():
        path = SCENARIOS_DIR / scenario_id
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Scenario '{scenario_id}' not found")
    data = yaml.safe_load(path.read_text())
    return {"id": scenario_id, "data": data}


@router.post("/analyze")
def run_analysis(req: AnalyzeRequest):
    """Run static flaw detection on an architecture."""
    try:
        arch = _resolve_architecture(req.architecture)
        findings = analyze(arch)
        load = estimate_load(arch)

        return {
            "architecture": arch.name,
            "findings": [
                {
                    "rule": f.rule,
                    "severity": f.severity,
                    "target": f.target,
                    "message": f.message,
                    "fix": f.fix,
                }
                for f in findings
            ],
            "load_estimate": {
                "rps": load.rps,
                "hold_ms": load.hold_ms,
                "utilization": {k: round(v, 4) for k, v in load.util.items()},
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Static analysis failed: {str(e)}")


@router.post("/simulate")
def run_simulation(req: SimulateRequest):
    """Run discrete-event simulation of an architecture under a scenario."""
    try:
        arch = _resolve_architecture(req.architecture)

        # Resolve scenario
        sc = _resolve_scenario(req.scenario, seed=req.seed)

        sim = Simulation(arch, sc)
        summary = sim.run()
        slo = sc.effective_slo(arch)

        # Sample client metrics across time for the real timeline curve
        timeline_points = [
            {
                "t": s.t,
                "err_rate": round(s.err_rate, 4),
                "rps": s.rps,
                "p50": round(s.p50, 1),
                "p99": round(s.p99, 1),
            }
            for s in sim.col.client_series
            if s.t % 5 == 0 or s.err_rate > 0
        ]

        return {
            "architecture": summary.architecture,
            "scenario": summary.scenario,
            "status": summary.status,
            "passed": summary.passed,
            "requests": summary.requests,
            "error_rate": summary.error_rate,
            "p50_ms": summary.p50_ms,
            "p99_ms": summary.p99_ms,
            "breach_seconds": summary.breach_seconds,
            "first_breach_s": summary.first_breach_s,
            "last_breach_s": summary.last_breach_s,
            "skipped_targets": summary.skipped_targets,
            "sim_events": summary.sim_events,
            "recovery_s": summary.recovery_s,
            "wall_time_s": summary.wall_time_s,
            "client_errors": dict(summary.client_errors),
            "node_errors": {n.id: n.errors for n in summary.nodes if n.errors},
            "nodes": [asdict(n) for n in summary.nodes],
            "cascade": summary.cascade,
            # Real SLO semantics: survival and recovery are distinct, and a
            # breach is never hidden behind a status-based `passed` flag.
            "slo_breached": summary.slo_breached,
            "survived": summary.survived,
            "recovered": summary.recovered,
            "fault_error_rate": summary.fault_error_rate,
            "post_fault_error_rate": summary.post_fault_error_rate,
            "seconds_out_of_slo": summary.seconds_out_of_slo,
            "duration_s": sc.duration_s,
            "warmup_s": sc.warmup_s,
            "slo_error_rate": slo.error_rate_max,
            "slo_p99_ms": slo.p99_ms_max,
            "slo_recover_within_s": slo.recover_within_s,
            "slo_window_s": slo.window_s,
            "architecture_fingerprint": architecture_fingerprint(arch),
            "timeline": timeline_points,
        }
    except HTTPException:
        raise
    except ValidationError as ve:
        raise HTTPException(status_code=400, detail=f"Validation failed: {str(ve)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Simulation failed: {str(e)}")


# ============================================================================
# Vertex AI Services
# ============================================================================

@router.post("/ai/text-to-model")
async def parse_text_to_model(req: TextToModelRequest):
    """
    Service 0: Create a new architecture from a prompt, or modify the current one
    while preserving everything the user did not explicitly change.
    """
    try:
        mode = req.mode or ("modify" if req.current_architecture is not None else "create")
        current = _resolve_architecture(req.current_architecture) if mode == "modify" else None
        if mode == "modify" and current is None:
            raise HTTPException(status_code=400, detail="current_architecture is required in modify mode")

        arch, yaml_str, warnings = await generate_architecture(
            req.description,
            override_name=req.name,
            mode=mode,
            current_architecture=current,
        )
        fingerprint = architecture_fingerprint(arch)
        diff = (
            {
                "changes": [],
                "fingerprint_before": None,
                "fingerprint_after": fingerprint,
            }
            if current is None
            else diff_architectures(current, arch)
        )
        return {
            "success": True,
            "mode": mode,
            "architecture": arch.model_dump(by_alias=True),
            "yaml": yaml_str,
            "name": arch.name,
            "node_count": len(arch.nodes),
            "edge_count": len(arch.edges),
            "fingerprint": fingerprint,
            "diff": diff,
            "warnings": warnings,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Vertex AI Text-to-Model failed: {str(e)}")


@router.post("/ai/adversary")
async def generate_adversary(req: AdversaryRequest):
    """Service 1: Generate multiple distinct adversarial scenarios."""
    try:
        arch = _resolve_architecture(req.architecture)
        entries, warnings = await generate_adversary_scenarios(
            arch,
            count=req.count,
            seed=req.seed,
            exclude_ids=req.exclude_ids,
            include_analysis=req.include_analysis,
        )
        first = entries[0] if entries else None
        return {
            "success": True,
            "count": len(entries),
            "scenarios": entries,
            "warnings": warnings,
            "scenario_source": first.get("source") if first else "fallback",
            # Backward compatibility: the first scenario is also exposed flat.
            "scenario": first["scenario"] if first else None,
            "yaml": first["yaml"] if first else "",
            "hypothesis": first["hypothesis"] if first else "",
            "target_nodes": first["target_nodes"] if first else [],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Vertex AI Adversary generation failed: {str(e)}")


@router.post("/ai/remediate")
async def remediate(req: RemediateRequest):
    """Service 2: Propose multiple alternative, discrete fixes."""
    try:
        arch = _resolve_architecture(req.architecture)
        fingerprint = req.architecture_fingerprint or architecture_fingerprint(arch)
        scenario = _resolve_scenario(req.scenario) if req.scenario else None

        fixes: List[Dict[str, Any]] = []
        verification: Dict[str, Any] = {"status": "no_verified_fix", "results": []}
        rejection = None
        rounds = max(1, min(int(req.rounds or 3), 3))
        deadline = time.monotonic() + 50.0  # keep the whole pipeline under ~60s
        round_index = 0
        for round_index in range(rounds):
            if round_index and time.monotonic() > deadline:
                break
            fixes = await generate_remediation_fixes(
                arch,
                req.simulation_result,
                findings=req.findings,
                count=req.count,
                base_fingerprint=fingerprint,
                rejection_reason=rejection,
                # Coverage retry only on the first round to stay inside 60s.
                prompt_attempts=2 if round_index == 0 else 1,
            )
            if scenario is None:
                verification = {
                    "status": "unverified",
                    "reason": "No scenario was supplied with the remediation request.",
                    "results": [],
                }
                break
            verification = await asyncio.to_thread(
                verify_fixes,
                arch,
                scenario,
                fixes,
                req.simulation_result,
                scenario.seed,
                True,  # process pool: one process per simulated candidate
            )
            if verification.get("status") == "verified":
                break
            if fixes and all(f.get("source") == "fallback" for f in fixes):
                # Deterministic fallback does not react to rejection reasons.
                break
            if time.monotonic() > deadline:
                break
            evidence = failure_report(req.simulation_result, verification, arch, fixes)
            rejection = verification_reason(verification, evidence)

        first = fixes[0] if fixes else None
        return {
            "success": True,
            "count": len(fixes),
            "base_fingerprint": fingerprint,
            "fixes": fixes,
            "status": verification.get("status", "unverified"),
            "verification": verification,
            "rounds_used": round_index + 1 if fixes else 0,
            # Backward compatibility with the single-fix response.
            "diagnosis": first["diagnosis"] if first else "",
            "root_cause_node": first["root_cause_node"] if first else "",
            "recommended_pattern": first["recommended_pattern"] if first else "",
            "patched_architecture": first["patched_architecture"] if first else arch.model_dump(by_alias=True),
            "patched_yaml": first["patched_yaml"] if first else "",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Vertex AI Remediation failed: {str(e)}")


# ============================================================================
# Architecture patching, validation and comparison
# ============================================================================


@router.post("/architectures/patch")
def patch_architecture(req: PatchRequest):
    """Apply one or more discrete fixes atomically, with fingerprint protection."""
    arch = _resolve_architecture(req.architecture)
    try:
        patched, diff, applied = apply_architecture_patch(arch, req.patch)
    except PatchConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (PatchValidationError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    applied_ids = list(applied)
    for fix_id in req.fix_ids:
        if fix_id not in applied_ids:
            applied_ids.append(fix_id)
    return {
        "architecture": patched.model_dump(by_alias=True),
        "yaml": yaml.dump(patched.model_dump(by_alias=True), sort_keys=False),
        "fingerprint": architecture_fingerprint(patched),
        "diff": diff,
        "applied_fix_ids": applied_ids,
        "validation": {"valid": True, "findings": _finding_dicts(patched)},
    }


@router.post("/architectures/validate")
def validate_architecture(req: AnalyzeRequest):
    """Validate an architecture and return its normalized form and static findings."""
    arch = _resolve_architecture(req.architecture)
    return {
        "valid": True,
        "architecture": arch.model_dump(by_alias=True),
        "yaml": yaml.dump(arch.model_dump(by_alias=True), sort_keys=False),
        "fingerprint": architecture_fingerprint(arch),
        "findings": _finding_dicts(arch),
    }


_NO_RECOVERY_DELTA = 1_000_000.0


def _delta(before: Any, after: Any, *, sentinel: bool = False) -> float:
    if before is None and after is None:
        return 0.0
    if sentinel:
        if before is None:
            return _NO_RECOVERY_DELTA
        if after is None:
            return -_NO_RECOVERY_DELTA
    return float(after) - float(before)


def _breach_delta(before: Any, after: Any) -> float | None:
    if before is None or after is None:
        return None
    return float(after) - float(before)


def _finding_rows(findings: List[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "rule": f.rule,
            "severity": f.severity,
            "target": f.target,
            "message": f.message,
            "fix": f.fix,
        }
        for f in findings
    ]


@router.post("/compare")
def compare_architectures_route(req: CompareRequest):
    """Run the same scenarios against two architectures and summarize the delta."""
    try:
        before = _resolve_architecture(req.before)
        after = _resolve_architecture(req.after)
        scenarios = [_resolve_scenario(sc, seed=req.seed) for sc in req.scenarios]
        result = compare_architectures(before, after, scenarios)

        rows = []
        for scenario, run_before, run_after in zip(scenarios, result.runs_a, result.runs_b):
            rank_before, rank_after = RANK[run_before.status], RANK[run_after.status]
            status_change = "worse" if rank_after > rank_before else "better" if rank_after < rank_before else "same"
            rows.append({
                "scenario": yaml.dump(scenario.model_dump(), sort_keys=False),
                "before_run": asdict(run_before),
                "after_run": asdict(run_after),
                "delta": {
                    "status_change": status_change,
                    "error_rate_delta": _delta(run_before.error_rate, run_after.error_rate),
                    "p99_delta_ms": _delta(run_before.p99_ms, run_after.p99_ms),
                    "recovery_delta_s": _delta(run_before.recovery_s, run_after.recovery_s, sentinel=True),
                    "breach_seconds_delta": _delta(run_before.breach_seconds, run_after.breach_seconds),
                    "first_breach_delta_s": _breach_delta(run_before.first_breach_s, run_after.first_breach_s),
                    "last_breach_delta_s": _breach_delta(run_before.last_breach_s, run_after.last_breach_s),
                },
            })

        return {
            "before": {
                "architecture": yaml.dump(before.model_dump(by_alias=True), sort_keys=False),
                "fingerprint": architecture_fingerprint(before),
            },
            "after": {
                "architecture": yaml.dump(after.model_dump(by_alias=True), sort_keys=False),
                "fingerprint": architecture_fingerprint(after),
            },
            "scenarios": rows,
            "findings": {
                "new": _finding_rows(result.new_findings),
                "resolved": _finding_rows(result.resolved_findings),
            },
            "utilization": [
                {"node": node, "before": before_util, "after": after_util}
                for node, before_util, after_util in result.util_rows
            ],
            "summary": {
                "regressions": len(result.regressions),
                "improvements": len(result.improvements),
                "new_findings": len(result.new_findings),
                "resolved_findings": len(result.resolved_findings),
            },
            "markdown": comparison_markdown(result),
        }
    except HTTPException:
        raise
    except ValidationError as ve:
        raise HTTPException(status_code=400, detail=f"Validation failed: {str(ve)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Comparison failed: {str(e)}")
