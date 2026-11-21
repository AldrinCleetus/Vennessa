"""Regression tests for verified remediation, findings contract and honest verdicts."""

from __future__ import annotations

import pytest

from app.ai.adversary import _fallback_scenarios
from app.ai.remediator import coverage_reason, required_findings
from app.core.engine import run
from app.core.fix_validation import validate_fix
from app.core.model import Architecture
from app.core.patch import PatchValidationError, apply_architecture_patch
from app.core.scenario import Scenario
from app.core.verification import (
    compare_outcomes,
    failure_report,
    outcome,
    verification_reason,
    verify_fixes,
)


def base_arch() -> dict:
    return {
        "name": "retry_storm",
        "slo": {"error_rate_max": 0.01, "p99_ms_max": 2500, "window_s": 30},
        "traffic": {"sources": [{"name": "users", "target": "api", "rps": 100}]},
        "nodes": [
            {"id": "api", "replicas": 4, "concurrency_per_replica": 30,
             "queue_per_replica": 20, "latency_ms": {"p50": 5, "p99": 20}},
            {"id": "order", "replicas": 4, "concurrency_per_replica": 30,
             "queue_per_replica": 20, "latency_ms": {"p50": 10, "p99": 40}},
            {"id": "db", "type": "datastore", "replicas": 1, "concurrency_per_replica": 40,
             "queue_per_replica": 40, "latency_ms": {"p50": 20, "p99": 80}},
        ],
        "edges": [
            {"from": "api", "to": "order", "timeout_ms": 2000,
             "retry": {"attempts": 2, "backoff": "exponential", "base_ms": 300, "jitter": True}},
            {"from": "order", "to": "db", "timeout_ms": 400,
             "retry": {"attempts": 1, "backoff": "exponential", "base_ms": 300, "jitter": True}},
        ],
    }


def stress_scenario() -> dict:
    return {
        "name": "db_latency",
        "duration_s": 180,
        "warmup_s": 10,
        "seed": 42,
        "events": [{"at_s": 50, "action": "slow", "target": "db", "factor": 4, "duration_s": 50}],
    }


def patched(ops: list[dict]) -> Architecture:
    arch = Architecture.model_validate(base_arch())
    result, _, _ = apply_architecture_patch(arch, {"ops": ops})
    return result


def codes(ops: list[dict], arch: Architecture | None = None) -> set[str]:
    """Codes that BLOCK the fix (pre-existing conditions are only advisories)."""
    arch = arch or Architecture.model_validate(base_arch())
    try:
        result, _, _ = apply_architecture_patch(arch, {"ops": ops})
    except PatchValidationError:
        # Schema-invalid paths are rejected before the simulator ever sees them.
        return {"schema_path"}
    return {
        i.code for i in validate_fix(arch, result, ops) if i.blocking
    }


# ---------------------------------------------------------------- validation rules


def test_validation_rate_limit_above_offered_load_rejected():
    assert "rate_limit_below_load" in codes([
        {"op": "set", "path": "/nodes/api/rate_limit_rps", "value": 100}
    ])


def test_validation_queue_exceeds_timeout_rejected():
    assert "queue_exceeds_timeout" in codes([
        {"op": "set", "path": "/nodes/db/queue_per_replica", "value": 500}
    ])


def test_validation_timeout_inversion_rejected():
    assert "timeout_inversion" in codes([
        {"op": "set", "path": "/edges/order->db/timeout_ms", "value": 8000}
    ])


def test_validation_multi_layer_retry_rejected():
    # Start from a design where only the deepest edge retries, then introduce a
    # second retry layer at the caller — the patch is what makes this blocking.
    data = base_arch()
    data["edges"][0]["retry"]["attempts"] = 0
    arch = Architecture.model_validate(data)
    ops = [{"op": "set", "path": "/edges/api->order/retry/attempts", "value": 1}]
    assert "multi_layer_retry" in codes(ops, arch)


def test_validation_unbounded_amplification_rejected():
    assert "unbounded_amplification" in codes([
        {"op": "set", "path": "/edges/order->db/retry/jitter", "value": False}
    ])


def test_validation_retry_attempts_and_cosmetic_backoff_rejected():
    ops = [
        {"op": "set", "path": "/edges/api->order/retry/attempts", "value": 3},
        {"op": "set", "path": "/edges/api->order/retry/base_ms", "value": 100},
    ]
    found = codes(ops)
    assert "retry_attempts_too_high" in found
    assert "backoff_cosmetic" in found


def test_validation_middle_tier_undersized_rejected():
    assert "middle_tier_undersized" in codes([
        {"op": "set", "path": "/nodes/api/concurrency_per_replica", "value": 1}
    ])


def test_validation_unknown_schema_path_rejected():
    assert "schema_path" in codes([
        {"op": "set", "path": "/nodes/missing/replicas", "value": 2}
    ])


def test_pre_existing_condition_is_advisory_not_blocking():
    """Regression: adding replicas to a node with a pre-existing long queue must
    not be rejected — the simulator, not the heuristic, decides if it helps."""
    data = base_arch()
    data["nodes"][2]["queue_per_replica"] = 500
    arch = Architecture.model_validate(data)
    ops = [{"op": "set", "path": "/nodes/db/replicas", "value": 2}]
    assert "queue_exceeds_timeout" not in codes(ops, arch)
    result, _, _ = apply_architecture_patch(arch, {"ops": ops})
    queue_issues = [
        i for i in validate_fix(arch, result, ops) if i.code == "queue_exceeds_timeout"
    ]
    assert queue_issues and all(i.blocking is False for i in queue_issues)


# ---------------------------------------------------------------- findings contract


def test_wrong_edge_for_top_finding_is_rejected():
    findings = [
        {"rule": "retry_amplification", "severity": "high", "target": "db", "message": "16x into db"},
        {"rule": "retry_no_backoff", "severity": "high", "target": "api->order", "message": "no backoff"},
    ]
    wrong = [{"fix_id": "f1", "valid": True,
              "patch": {"ops": [{"op": "set", "path": "/edges/api->order/retry/attempts", "value": 2}]}}]
    assert coverage_reason(wrong, required_findings(findings), Architecture.model_validate(base_arch())) is not None

    right = [{"fix_id": "f2", "valid": True,
              "patch": {"ops": [{"op": "set", "path": "/edges/order->db/retry/attempts", "value": 1}]}}]
    assert coverage_reason(right, required_findings(findings), Architecture.model_validate(base_arch())) is None


# ---------------------------------------------------------------- verification


def test_deliberately_bad_fix_is_worse_and_not_verified():
    original = outcome(run(Architecture.model_validate(base_arch()), Scenario.model_validate(stress_scenario())))
    bad = dict(original)
    bad.update(status="no_recovery", error_rate=original["error_rate"] + 0.2,
               seconds_out_of_slo=original["seconds_out_of_slo"] + 50,
               post_fault_error_rate=original["post_fault_error_rate"] + 0.1)
    scored = compare_outcomes(original, bad)
    assert scored["classification"] == "worse"
    assert scored["verified"] is False


def test_slo_breach_is_never_reported_as_survived():
    arch = Architecture.model_validate(base_arch())
    arch.traffic.sources[0].rps = 5000
    result = run(arch, Scenario.model_validate(stress_scenario()))
    assert result.error_rate > 0.037  # > 3.7x the 1% SLO
    assert result.slo_breached is True
    assert result.survived is False


def test_verification_runs_candidates_in_parallel_and_marks_noop():
    arch = Architecture.model_validate(base_arch())
    sc = Scenario.model_validate(stress_scenario())
    baseline = outcome(run(arch, sc))
    fixes = [
        {"fix_id": "noop", "valid": True, "patch": {"ops": [
            {"op": "set", "path": "/nodes/db/replicas", "value": 1}
        ]}},
    ]
    result = verify_fixes(arch, sc, fixes, parallel=True, seed=42)
    assert result["attempted"] == 1
    assert result["status"] in {"verified", "no_verified_fix"}
    assert fixes[0]["verification"]["verified"] is False  # identical config cannot improve


# ---------------------------------------------------------------- scenarios & determinism


def test_fallback_scenario_is_marked_and_finding_derived():
    arch = Architecture.model_validate(base_arch())
    arch.edges[1].retry.attempts = 3
    arch.edges[1].retry.backoff = "none"
    entries = _fallback_scenarios(arch, 3, 42)
    assert entries
    assert all(e["source"] == "fallback" for e in entries)
    assert any(
        ev["action"] == "slow"
        for e in entries
        for ev in e["scenario"]["events"]
    )


def test_determinism_all_metrics_equal_for_same_seed():
    a, sc = Architecture.model_validate(base_arch()), Scenario.model_validate(stress_scenario())
    x, y = outcome(run(a, sc)), outcome(run(a, sc))
    assert x == y


def test_failure_report_carries_original_and_patched_yaml_diff():
    arch = Architecture.model_validate(base_arch())
    simulation = {"error_rate": 0.2, "post_fault_error_rate": 0.01,
                  "nodes": [], "client_errors": {}}
    fixes = [{"fix_id": "f1", "patched_yaml": "nodes:\n  - id: api\n    replicas: 6\n"}]
    verification = {"results": [
        {"fix_id": "f1", "verification": {"classification": "neutral"}}
    ]}
    evidence = failure_report(simulation, verification, arch, fixes)
    assert evidence["original_architecture_yaml"]
    option = evidence["attempted_options"][0]
    assert option["patched_yaml"].startswith("nodes:")
    assert "replicas" in option["yaml_diff"]
    reason = verification_reason(verification, evidence)
    assert "yaml_diff" in reason and "exact patch" in reason
