"""Route-level tests for patching, comparison and simulation extras."""

import pytest

from app.main import app

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


def arch_data(*, api_replicas: int = 2, api_concurrency: int = 20, rps: int = 50) -> dict:
    return {
        "name": "route_test",
        "slo": {"error_rate_max": 0.01, "p99_ms_max": 2000, "window_s": 30},
        "traffic": {"sources": [{"name": "users", "target": "lb", "rps": rps}]},
        "nodes": [
            {"id": "lb", "type": "load_balancer", "replicas": 2, "concurrency_per_replica": 1000},
            {
                "id": "api", "type": "service", "replicas": api_replicas,
                "concurrency_per_replica": api_concurrency, "queue_per_replica": 50,
            },
            {"id": "db", "type": "datastore", "replicas": 1, "concurrency_per_replica": 15},
        ],
        "edges": [
            {"from": "lb", "to": "api", "timeout_ms": 3000},
            {"from": "api", "to": "db", "timeout_ms": 1500},
        ],
    }


def scenario_data() -> dict:
    return {
        "name": "db_restart",
        "description": "Restart the database briefly",
        "duration_s": 60,
        "warmup_s": 5,
        "seed": 7,
        "events": [{"at_s": 20, "action": "restart", "target": "db"}],
    }


def test_patch_route_applies_ops_and_returns_diff():
    response = client.post("/api/architectures/patch", json={
        "architecture": arch_data(),
        "patch": {
            "ops": [
                {"op": "set", "path": "/nodes/api/replicas", "value": 5},
                {"op": "set", "path": "/edges/api->db/retry/jitter", "value": True},
            ],
            "source_fix_id": "fix_123",
        },
        "fix_ids": ["fix_123"],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["architecture"]["nodes"][1]["replicas"] == 5
    assert body["architecture"]["edges"][1]["retry"]["jitter"] is True
    assert body["architecture"]["nodes"][2]["id"] == "db"
    assert body["diff"]["fingerprint_before"] != body["diff"]["fingerprint_after"]
    assert any(c["path"] == "/nodes/api/replicas" for c in body["diff"]["changes"])
    assert body["applied_fix_ids"] == ["fix_123"]
    assert body["validation"]["valid"] is True
    assert "nodes:" in body["yaml"]


def test_patch_route_conflict_returns_409():
    response = client.post("/api/architectures/patch", json={
        "architecture": arch_data(),
        "patch": {
            "base_fingerprint": "definitely-stale",
            "ops": [{"op": "set", "path": "/name", "value": "other"}],
        },
    })
    assert response.status_code == 409, response.text
    assert "changed since the patch was generated" in response.json()["detail"]


def test_patch_route_invalid_result_returns_400():
    response = client.post("/api/architectures/patch", json={
        "architecture": arch_data(),
        "patch": {"ops": [{"op": "set", "path": "/nodes/api/startup/requires", "value": ["nope"]}]},
    })
    assert response.status_code == 400, response.text
    assert "unknown node" in response.json()["detail"]


def test_compare_route_returns_runs_deltas_and_markdown():
    before = arch_data(api_replicas=1, api_concurrency=3, rps=200)
    after = arch_data(api_replicas=6, api_concurrency=30, rps=200)
    response = client.post("/api/compare", json={
        "before": before,
        "after": after,
        "scenarios": [scenario_data()],
        "seed": 7,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["before"]["fingerprint"]) == 64
    assert len(body["after"]["fingerprint"]) == 64
    assert body["before"]["fingerprint"] != body["after"]["fingerprint"]
    assert "name: route_test" in body["before"]["architecture"]
    assert len(body["scenarios"]) == 1
    row = body["scenarios"][0]
    assert "name: db_restart" in row["scenario"]
    assert row["before_run"]["status"] in {"ok", "degraded", "slow_recovery", "no_recovery"}
    assert row["after_run"]["status"] in {"ok", "degraded", "slow_recovery", "no_recovery"}
    assert row["delta"]["status_change"] in {"better", "worse", "same"}
    assert isinstance(row["delta"]["error_rate_delta"], (int, float))
    assert isinstance(row["delta"]["p99_delta_ms"], (int, float))
    assert set(body["findings"]) == {"new", "resolved"}
    assert body["utilization"]
    assert set(body["summary"]) == {
        "regressions", "improvements", "new_findings", "resolved_findings",
    }
    assert "vs" in body["markdown"]


def test_validate_route_and_simulate_response_extras():
    validation = client.post("/api/architectures/validate", json={"architecture": arch_data()})
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True
    assert "findings" in validation.json()

    simulation = client.post("/api/simulate", json={
        "architecture": arch_data(),
        "scenario": {**scenario_data(), "duration_s": 30, "warmup_s": 5},
    })
    assert simulation.status_code == 200, simulation.text
    body = simulation.json()
    for key in (
        "first_breach_s", "last_breach_s", "skipped_targets", "sim_events",
        "slo_p99_ms", "slo_recover_within_s", "slo_window_s", "architecture_fingerprint",
    ):
        assert key in body
    assert body["slo_p99_ms"] == 2000
    assert body["slo_window_s"] == 30
    assert len(body["architecture_fingerprint"]) == 64
