"""Tests for atomic architecture patching, diffing and fingerprints."""

import copy

import pytest

from app.core.model import Architecture
from app.core.patch import (
    PatchConflictError,
    PatchValidationError,
    apply_architecture_patch,
    architecture_fingerprint,
    diff_architectures,
)


def sample_data() -> dict:
    return {
        "name": "patch_test",
        "description": "original description",
        "slo": {"error_rate_max": 0.01, "p99_ms_max": 2000, "window_s": 30},
        "traffic": {"sources": [{"name": "users", "target": "lb", "rps": 100}]},
        "nodes": [
            {"id": "lb", "type": "load_balancer", "replicas": 2, "concurrency_per_replica": 1000},
            {
                "id": "api", "type": "service", "replicas": 2,
                "concurrency_per_replica": 20, "queue_per_replica": 50,
                "startup": {"requires": ["db"]},
            },
            {"id": "db", "type": "datastore", "replicas": 1, "concurrency_per_replica": 15},
        ],
        "edges": [
            {"from": "lb", "to": "api", "timeout_ms": 3000},
            {"from": "api", "to": "db", "timeout_ms": 1500, "retry": {"attempts": 2}},
        ],
    }


def sample_arch() -> Architecture:
    return Architecture.model_validate(sample_data())


def test_fingerprint_is_canonical_and_version_sensitive():
    a = sample_arch()
    reordered = copy.deepcopy(sample_data())
    reordered = dict(reversed(list(reordered.items())))
    assert architecture_fingerprint(a) == architecture_fingerprint(Architecture.model_validate(reordered))

    changed = sample_data()
    changed["nodes"][1]["replicas"] = 4
    assert architecture_fingerprint(a) != architecture_fingerprint(Architecture.model_validate(changed))


def test_diff_uses_identity_paths():
    arch = sample_arch()
    patched, _, _ = apply_architecture_patch(arch, {
        "ops": [
            {"op": "set", "path": "/nodes/api/replicas", "value": 4},
            {"op": "set", "path": "/edges/api->db/retry/jitter", "value": True},
            {"op": "add", "path": "/traffic/sources", "value": {"name": "admin", "target": "lb", "rps": 5}},
        ],
    })
    diff = diff_architectures(arch, patched)
    paths = {c["path"] for c in diff["changes"]}
    assert "/nodes/api/replicas" in paths
    assert "/edges/api->db/retry/jitter" in paths
    assert "/traffic/sources/admin" in paths
    assert diff["fingerprint_before"] == architecture_fingerprint(arch)
    assert diff["fingerprint_after"] == architecture_fingerprint(patched)


def test_set_deep_merges_and_preserves_unrelated_fields():
    arch = sample_arch()
    patched, _, _ = apply_architecture_patch(arch, {
        "ops": [
            {"op": "set", "path": "/nodes/api/health_check", "value": {"interval_s": 5}},
            {"op": "set", "path": "/slo/error_rate_max", "value": 0.05},
        ],
    })
    assert patched.node("api").health_check.interval_s == 5
    assert patched.node("api").health_check.unhealthy_after == 3  # default preserved
    assert patched.node("api").replicas == 2
    assert patched.node("db").replicas == 1
    assert patched.description == "original description"
    assert patched.slo.error_rate_max == 0.05
    assert patched.slo.p99_ms_max == 2000
    assert arch.node("api").health_check is None  # original untouched


def test_add_nodes_edges_and_sources():
    arch = sample_arch()
    patched, _, applied = apply_architecture_patch(arch, {
        "base_fingerprint": architecture_fingerprint(arch),
        "source_fix_id": "fix_1",
        "ops": [
            {"op": "add", "path": "/nodes", "value": {
                "id": "cache", "type": "cache", "replicas": 2,
                "concurrency_per_replica": 500, "warm_hit_rate": 0.9,
            }},
            {"op": "add", "path": "/edges", "value": {
                "from": "api", "to": "cache", "timeout_ms": 200,
            }},
            {"op": "add", "path": "/nodes", "value": {
                "id": "db-r1", "type": "datastore", "replicas": 1,
                "concurrency_per_replica": 15,
            }},
            {"op": "set", "path": "/nodes/db/read_replicas", "value": ["db-r1"]},
        ],
    })
    assert patched.has_node("cache")
    assert ("api", "cache") in {e.key for e in patched.edges}
    assert patched.node("db").read_replicas == ["db-r1"]
    assert applied == ["fix_1"]


def test_remove_node_cleans_references():
    data = sample_data()
    data["nodes"][0]["startup"] = {"requires": ["api"]}
    data["traffic"]["sources"].append({"name": "admin", "target": "api", "rps": 5})
    arch = Architecture.model_validate(data)

    patched, _, _ = apply_architecture_patch(arch, {
        "ops": [{"op": "remove", "path": "/nodes/api"}],
    })
    assert not patched.has_node("api")
    assert all(e.from_ != "api" and e.to != "api" for e in patched.edges)
    assert [s.name for s in patched.traffic.sources] == ["users"]
    assert patched.node("lb").startup.requires == []


def test_conflict_and_invalid_patch_are_typed():
    arch = sample_arch()
    with pytest.raises(PatchConflictError):
        apply_architecture_patch(arch, {
            "base_fingerprint": "stale", "ops": [{"op": "set", "path": "/name", "value": "x"}],
        })
    with pytest.raises(PatchValidationError):
        apply_architecture_patch(arch, {
            "ops": [{"op": "set", "path": "/nodes/missing/replicas", "value": 3}],
        })
    with pytest.raises(PatchValidationError):
        apply_architecture_patch(arch, {"ops": [{"op": "frobnicate", "path": "/name"}]})


def test_patch_is_atomic():
    arch = sample_arch()
    before = architecture_fingerprint(arch)
    with pytest.raises(PatchValidationError):
        apply_architecture_patch(arch, {
            "ops": [
                {"op": "set", "path": "/name", "value": "changed"},
                {"op": "set", "path": "/nodes/nope/replicas", "value": 3},
            ],
        })
    assert architecture_fingerprint(arch) == before
