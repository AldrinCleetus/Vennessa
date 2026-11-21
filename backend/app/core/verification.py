"""Verify remediation candidates by re-simulating them under identical conditions.

This module is the honesty gate for the fix pipeline: no fix is recommended
unless its effect has actually been measured in the simulator.
"""

from __future__ import annotations

import difflib
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List

import yaml

from .engine import Simulation
from .metrics import RunSummary
from .model import Architecture
from .patch import apply_architecture_patch
from .scenario import Scenario

VERDICT_RANK = {"ok": 0, "degraded": 1, "slow_recovery": 2, "no_recovery": 3}
LOWER_IS_BETTER = (
    "error_rate",
    "fault_error_rate",
    "post_fault_error_rate",
    "p99_ms",
    "seconds_out_of_slo",
)
OUTCOME_KEYS = ("status", "survived", "recovered", "slo_breached", *LOWER_IS_BETTER)


def outcome(summary: RunSummary) -> Dict[str, Any]:
    """Compact, comparable scorecard for one simulation run."""
    return {
        "status": summary.status,
        "survived": summary.survived,
        "recovered": summary.recovered,
        "slo_breached": summary.slo_breached,
        "error_rate": summary.error_rate,
        "fault_error_rate": summary.fault_error_rate,
        "post_fault_error_rate": summary.post_fault_error_rate,
        "p99_ms": summary.p99_ms,
        "seconds_out_of_slo": summary.seconds_out_of_slo,
        "requests": summary.requests,
        "recovery_s": summary.recovery_s,
    }


def outcome_from_result(result: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Normalize an API `/simulate` result for comparison with fresh runs."""
    if not isinstance(result, dict):
        return None
    base = {
        "status": result.get("status", "ok"),
        "survived": result.get("survived", not result.get("slo_breached", False)),
        "recovered": result.get("recovered", True),
        "slo_breached": result.get("slo_breached", False),
        "error_rate": result.get("error_rate", 0.0),
        "fault_error_rate": result.get("fault_error_rate", result.get("error_rate", 0.0)),
        "post_fault_error_rate": result.get("post_fault_error_rate", result.get("error_rate", 0.0)),
        "p99_ms": result.get("p99_ms", 0.0),
        "seconds_out_of_slo": result.get(
            "seconds_out_of_slo", result.get("breach_seconds", 0)
        ),
        "requests": result.get("requests", 0),
        "recovery_s": result.get("recovery_s"),
    }
    return {k: base[k] for k in (*OUTCOME_KEYS, "requests", "recovery_s")}


def simulate_patch(
    architecture: Dict[str, Any],
    scenario: Dict[str, Any],
    patch: Dict[str, Any] | None = None,
    seed: int | None = None,
) -> Dict[str, Any]:
    """Run one simulation. Module level so it is picklable by ProcessPoolExecutor."""
    arch = Architecture.model_validate(architecture)
    sc_data = dict(scenario)
    if seed is not None:
        sc_data["seed"] = seed
    sc = Scenario.model_validate(sc_data)
    if patch and patch.get("ops"):
        arch, _, _ = apply_architecture_patch(arch, patch)
    return outcome(Simulation(arch, sc).run())


def _worker(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return simulate_patch(
            payload["architecture"], payload["scenario"], payload.get("patch"), payload.get("seed")
        )
    except Exception as exc:  # an unusable patch is evidence, not a pipeline crash
        return {
            "status": "no_recovery", "survived": False, "recovered": False,
            "slo_breached": True, "error_rate": 0.0, "fault_error_rate": 0.0,
            "post_fault_error_rate": 0.0, "p99_ms": 0.0, "seconds_out_of_slo": 0,
            "requests": 0, "recovery_s": None, "error": str(exc),
        }


def compare_outcomes(original: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Score a candidate against the original run.

    Verified requires a better verdict, or strictly better error rate AND
    seconds-out-of-SLO with no regression in post-fault behaviour.
    """
    deltas: Dict[str, float] = {}
    better: Dict[str, bool] = {}
    for key in LOWER_IS_BETTER:
        before, after = float(original.get(key, 0) or 0), float(candidate.get(key, 0) or 0)
        deltas[key] = after - before
        better[key] = after < before - 1e-9

    verdict_before = VERDICT_RANK.get(str(original.get("status")), 3)
    verdict_after = VERDICT_RANK.get(str(candidate.get("status")), 3)
    verdict_improved = verdict_after < verdict_before
    no_post_regression = (
        float(candidate.get("post_fault_error_rate", 0) or 0)
        <= float(original.get("post_fault_error_rate", 0) or 0) + 1e-9
    )
    strict_metric_gain = better["error_rate"] and better["seconds_out_of_slo"]

    verified = verdict_improved or (strict_metric_gain and no_post_regression)
    verdict_regressed = verdict_after > verdict_before
    metric_regressed = (
        deltas["error_rate"] > max(0.005, 0.05 * float(original.get("error_rate", 0) or 0))
        and not better["seconds_out_of_slo"]
    )
    classification = "improved" if verified else "worse" if verdict_regressed or metric_regressed else "neutral"

    return {
        "verified": verified,
        "classification": classification,
        "verdict_improved": verdict_improved,
        "original": original,
        "candidate": candidate,
        "deltas": deltas,
        "better": better,
        "post_fault_regression": not no_post_regression,
    }


def _run_all(
    tasks: List[Dict[str, Any]], parallel: bool
) -> List[Dict[str, Any]]:
    if not tasks:
        return []
    if not parallel or len(tasks) == 1:
        return [_worker(t) for t in tasks]
    workers = min(4, os.cpu_count() or 1, len(tasks))
    results: List[Dict[str, Any] | None] = [None] * len(tasks)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_worker, task): i for i, task in enumerate(tasks)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [r if r is not None else {} for r in results]


def yaml_diff(original: str | None, patched: str | None, limit: int = 40) -> str:
    """Compact changed-line diff for the rejection prompt."""
    lines = difflib.unified_diff(
        (original or "").splitlines(), (patched or "").splitlines(),
        lineterm="", n=0,
    )
    return "\n".join(
        line for line in lines
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    )[:2000] or "(no YAML change)"


def failure_report(
    simulation_result: Dict[str, Any] | None,
    verification: Dict[str, Any] | None = None,
    architecture: Architecture | None = None,
    fixes: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Compact evidence package sent back to Gemini when nothing was verified."""
    result = simulation_result or {}
    nodes = []
    for node in result.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nodes.append({
            "id": node.get("id"),
            "peak_util": round(float(node.get("peak_util") or 0), 3),
            "max_queued": node.get("max_queued"),
            "min_up": node.get("min_up"),
            "errors": node.get("errors") or {},
        })
    rejections: Dict[str, int] = {}
    for reasons in (result.get("node_errors") or {}).values():
        if isinstance(reasons, dict):
            for reason, count in reasons.items():
                rejections[reason] = rejections.get(reason, 0) + int(count)
    for reason, count in (result.get("client_errors") or {}).items():
        rejections[f"client:{reason}"] = rejections.get(f"client:{reason}", 0) + int(count)

    original_yaml = (
        yaml.dump(architecture.model_dump(by_alias=True), sort_keys=False)
        if architecture is not None else None
    )
    fix_by_id = {f.get("fix_id"): f for f in (fixes or []) if isinstance(f, dict)}
    attempted: List[Dict[str, Any]] = []
    for item in (verification or {}).get("results", []):
        fix = fix_by_id.get(item.get("fix_id"), {})
        v = item.get("verification") or {}
        patched_yaml = fix.get("patched_yaml")
        attempted.append({
            "fix_id": item.get("fix_id"),
            "classification": v.get("classification"),
            "deltas": v.get("deltas"),
            "patch_ops": (fix.get("patch") or {}).get("ops"),
            "patched_yaml": patched_yaml,
            "yaml_diff": yaml_diff(original_yaml, patched_yaml),
            "validation_errors": fix.get("validation_errors"),
            "advisories": fix.get("advisories"),
        })
    evidence = {
        "original_outcome": outcome_from_result(result),
        "nodes": nodes,
        "rejections_by_reason": rejections,
        "post_fault_error_rate_persists": (
            float((result.get("post_fault_error_rate") or 0)) > float(result.get("slo_error_rate") or 0.01)
        ),
        "original_architecture_yaml": original_yaml,
        "attempted_options": attempted,
        "combination": (verification or {}).get("combination"),
    }
    return evidence


def verification_reason(verification: Dict[str, Any], evidence: Dict[str, Any]) -> str:
    """Human/model-readable rejection reason for the next Gemini round."""
    original = verification.get("original") or {}
    evidence_options = {o.get("fix_id"): o for o in evidence.get("attempted_options", [])}
    failed = []
    for item in verification.get("results", []):
        v = item.get("verification") or {}
        if v.get("classification") in {"worse", "neutral"}:
            d = v.get("deltas") or {}
            option = evidence_options.get(item.get("fix_id"), {})
            failed.append(
                f"{item.get('fix_id')} ({v.get('classification')}) error "
                f"{d.get('error_rate', 0):+.4f}, out-of-SLO "
                f"{d.get('seconds_out_of_slo', 0):+.0f}s, post-fault "
                f"{d.get('post_fault_error_rate', 0):+.4f}; ops="
                f"{json.dumps(option.get('patch_ops'), default=str)[:300]}; "
                f"yaml_diff=\n{option.get('yaml_diff')}"
            )
    return (
        "None of the previously proposed fixes were verified by re-simulation. "
        f"Baseline: error={original.get('error_rate')}, seconds_out_of_slo="
        f"{original.get('seconds_out_of_slo')}, status={original.get('status')}. "
        "Rejected options (with the exact patch and YAML change that was measured): "
        f"{'; '.join(failed) or 'all were neutral'}. "
        f"Node evidence: {evidence.get('nodes')}. "
        f"Rejections by reason: {evidence.get('rejections_by_reason')}. "
        f"Post-fault errors persist: {evidence.get('post_fault_error_rate_persists')}. "
        "Propose genuinely different mechanisms on the top finding's target; do not "
        "repeat a rejected patch or tweak its numbers cosmetically."
    )


def verify_fixes(
    architecture: Architecture,
    scenario: Scenario,
    fixes: List[Dict[str, Any]],
    simulation_result: Dict[str, Any] | None = None,
    seed: int | None = None,
    parallel: bool = True,
) -> Dict[str, Any]:
    """Apply each fix alone (plus the selected combination) and re-simulate.

    Mutates each fix dict with a `verification` block containing measured (not
    predicted) impact. Returns a pipeline-level status and best partial result.
    """
    arch_dict = architecture.model_dump(by_alias=True)
    sc_dict = scenario.model_dump(by_alias=True)
    if seed is not None:
        sc_dict["seed"] = seed

    original = outcome_from_result(simulation_result) or simulate_patch(arch_dict, sc_dict)
    # Even rejected candidates are measured: their numbers become the evidence
    # sent back to Gemini and the "best partial" shown when nothing verifies.
    with_patch = [
        f for f in fixes if isinstance(f, dict) and (f.get("patch") or {}).get("ops")
    ]

    outcomes = _run_all(
        [
            {
                "architecture": arch_dict,
                "scenario": sc_dict,
                "patch": f["patch"],
                "seed": seed,
            }
            for f in with_patch
        ],
        parallel,
    )
    scored: List[Dict[str, Any]] = []
    for fix, candidate_outcome in zip(with_patch, outcomes):
        verification = compare_outcomes(original, candidate_outcome)
        verification["scope"] = "single"
        verification["admissible"] = fix.get("valid") is not False
        if not verification["admissible"]:
            verification["verified"] = False
        fix["verification"] = verification
        scored.append({"fix_id": fix.get("fix_id"), "verification": verification})

    combo_patch = None
    admissible = [f for f in with_patch if f.get("valid") is not False]
    if admissible:
        ops: List[Dict[str, Any]] = []
        for fix in admissible:
            ops.extend((fix["patch"]).get("ops") or [])
        combo_patch = {
            "base_fingerprint": (admissible[0]["patch"]).get("base_fingerprint"),
            "ops": ops,
        }
        try:
            combo_outcome = simulate_patch(arch_dict, sc_dict, combo_patch, seed)
            combo = compare_outcomes(original, combo_outcome)
            combo["scope"] = "combination"
        except Exception as exc:  # patch conflicts must not abort verification
            combo = {
                "verified": False,
                "classification": "worse",
                "error": str(exc),
                "scope": "combination",
            }
    else:
        combo = {
            "verified": False,
            "classification": "neutral",
            "reason": "No valid patch operations were proposed.",
            "scope": "combination",
        }

    verified = [s for s in scored if s["verification"]["verified"] and s["verification"]["classification"] != "worse"]
    if combo.get("verified") and combo.get("classification") != "worse":
        verified.append({"fix_id": "combination", "verification": combo})

    def _score(item: Dict[str, Any]) -> float:
        v = item["verification"]
        d = v.get("deltas", {})
        return -(
            d.get("error_rate", 0) * 10
            + d.get("seconds_out_of_slo", 0) / 100
            + d.get("post_fault_error_rate", 0) * 5
            + d.get("p99_ms", 0) / 1000
        )

    # Best partial is reported even when nothing is verified, with an explicit flag.
    all_scored = list(scored) + ([{"fix_id": "combination", "verification": combo}] if with_patch else [])
    best_partial = max(all_scored, key=_score) if all_scored else None

    return {
        "original": original,
        "results": scored,
        "combination": combo,
        "verified_fix_ids": [s["fix_id"] for s in verified],
        "status": "verified" if verified else "no_verified_fix",
        "best_partial": best_partial,
        "attempted": len(with_patch),
    }
