from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from app.core.analyze import analyze, estimate_load
from app.core.engine import Simulation, run as run_sim
from app.core.model import Architecture, load_architecture
from app.core.scenario import Scenario, load_scenario
from app.ai.parser import text_to_architecture_yaml
from app.ai.adversary import generate_adversary_scenario
from app.ai.remediator import remediate_architecture

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
    current_architecture: Optional[Dict[str, Any]] = None


class AdversaryRequest(BaseModel):
    architecture: Dict[str, Any] | str


class RemediateRequest(BaseModel):
    architecture: Dict[str, Any] | str
    simulation_result: Dict[str, Any]


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
        if isinstance(req.scenario, str):
            path = SCENARIOS_DIR / f"{req.scenario}.yaml"
            if not path.exists():
                path = SCENARIOS_DIR / req.scenario
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"Scenario '{req.scenario}' not found")
            sc = load_scenario(path)
        else:
            sc = Scenario.model_validate(req.scenario)

        if req.seed is not None:
            sc.seed = req.seed

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
            "recovery_s": summary.recovery_s,
            "wall_time_s": summary.wall_time_s,
            "client_errors": dict(summary.client_errors),
            "node_errors": {n.id: n.errors for n in summary.nodes if n.errors},
            "nodes": [asdict(n) for n in summary.nodes],
            "cascade": summary.cascade,
            "slo_breached": not summary.passed,
            "duration_s": sc.duration_s,
            "warmup_s": sc.warmup_s,
            "slo_error_rate": slo.error_rate_max,
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
    Service 0: Converts a natural language architecture description into a validated
    Architecture YAML matching the reference format in ./backend/models/.
    """
    try:
        arch, yaml_str = await text_to_architecture_yaml(req.description, override_name=req.name)
        return {
            "success": True,
            "architecture": arch.model_dump(by_alias=True),
            "yaml": yaml_str,
            "name": arch.name,
            "node_count": len(arch.nodes),
            "edge_count": len(arch.edges),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Vertex AI Text-to-Model failed: {str(e)}")


@router.post("/ai/adversary")
async def generate_adversary(req: AdversaryRequest):
    """
    Service 1: AI Adversary - Analyzes architecture and generates a targeted stress
    and failure scenario to test weaknesses.
    """
    try:
        arch = _resolve_architecture(req.architecture)
        scenario, yaml_str, hypothesis, target_nodes = await generate_adversary_scenario(arch)
        return {
            "success": True,
            "scenario": scenario.model_dump(),
            "yaml": yaml_str,
            "hypothesis": hypothesis,
            "target_nodes": target_nodes,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Vertex AI Adversary generation failed: {str(e)}")


@router.post("/ai/remediate")
async def remediate(req: RemediateRequest):
    """
    Service 2: Solution Engine - Diagnoses simulation failure metrics and generates
    a targeted architectural fix and patched YAML.
    """
    try:
        arch = _resolve_architecture(req.architecture)
        diagnosis, root_cause, pattern, patched_arch, patched_yaml = await remediate_architecture(
            arch, req.simulation_result
        )
        return {
            "success": True,
            "diagnosis": diagnosis,
            "root_cause_node": root_cause,
            "recommended_pattern": pattern,
            "patched_architecture": patched_arch.model_dump(by_alias=True),
            "patched_yaml": patched_yaml,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Vertex AI Remediation failed: {str(e)}")
