"""Unit tests for Vertex AI integration services."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.model import Architecture, Node, Edge, Traffic, Source, SLO
from app.ai.parser import (
    clean_yaml_response,
    generate_architecture,
    text_to_architecture_yaml,
)
from app.ai.adversary import clean_yaml, generate_adversary_scenario, generate_adversary_scenarios
from app.ai.remediator import clean_json, generate_remediation_fixes, remediate_architecture


def sample_arch() -> Architecture:
    return Architecture(
        name="test_system",
        slo=SLO(error_rate_max=0.01, p99_ms_max=2000),
        traffic=Traffic(sources=[Source(name="users", target="lb", rps=100)]),
        nodes=[
            Node(id="lb", type="load_balancer", replicas=2, concurrency_per_replica=1000),
            Node(id="api", type="service", replicas=2, concurrency_per_replica=20, queue_per_replica=50),
            Node(id="db", type="datastore", replicas=1, concurrency_per_replica=15),
        ],
        edges=[
            Edge(**{"from": "lb", "to": "api", "timeout_ms": 3000}),
            Edge(**{"from": "api", "to": "db", "timeout_ms": 1500}),
        ]
    )


def test_clean_yaml_response():
    markdown = "```yaml\nname: sample\nnodes: []\n```"
    assert clean_yaml_response(markdown) == "name: sample\nnodes: []"


def test_clean_json():
    markdown = "```json\n{\"diagnosis\": \"ok\"}\n```"
    assert clean_json(markdown) == "{\"diagnosis\": \"ok\"}"


def test_text_to_architecture_yaml_mocked():
    sample_yaml = """
name: generated_app
slo:
  error_rate_max: 0.01
  p99_ms_max: 2000
traffic:
  sources:
    - name: web
      target: ingress
      rps: 50
nodes:
  - id: ingress
    type: load_balancer
    replicas: 1
    concurrency_per_replica: 500
  - id: backend
    type: service
    replicas: 2
    concurrency_per_replica: 20
edges:
  - from: ingress
    to: backend
    timeout_ms: 2000
"""
    mock_resp = MagicMock()
    mock_resp.text = f"```yaml\n{sample_yaml}\n```"

    with patch("app.ai.client.get_vertex_client") as mock_client_factory:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
        mock_client_factory.return_value = mock_client

        arch, yaml_out = asyncio.run(text_to_architecture_yaml("A simple web app"))
        assert arch.name == "generated_app"
        assert len(arch.nodes) == 2
        assert len(arch.edges) == 1
        assert "generated_app" in yaml_out


def test_adversary_scenario_mocked():
    arch = sample_arch()
    sample_scenario_yaml = """
name: test_attack
description: "Overloading DB with latency spike"
duration_s: 180
warmup_s: 20
traffic:
  multiplier: 2.0
events:
  - at_s: 50
    action: slow
    target: db
    factor: 4.0
    duration_s: 30
"""
    mock_resp = MagicMock()
    mock_resp.text = f"```yaml\n{sample_scenario_yaml}\n```"

    with patch("app.ai.client.get_vertex_client") as mock_client_factory:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
        mock_client_factory.return_value = mock_client

        scenario, yaml_out, hypothesis, targets = asyncio.run(generate_adversary_scenario(arch))
        assert scenario.name == "test_attack"
        assert targets == ["db"]
        assert "db" in yaml_out


def test_generate_content_falls_back_to_api_key():
    from app.ai import client as ai_client

    primary = MagicMock()
    primary.aio.models.generate_content = AsyncMock(
        side_effect=RuntimeError("Your default credentials were not found")
    )
    fallback = MagicMock()
    fallback.aio.models.generate_content = AsyncMock(
        return_value=MagicMock(text="fallback response")
    )

    with (
        patch.object(ai_client, "API_KEY", "test-key"),
        patch.object(ai_client, "PREFER_API_KEY", False),
        patch.object(ai_client, "get_vertex_client", return_value=primary),
        patch.object(ai_client, "get_api_key_client", return_value=fallback),
    ):
        result = asyncio.run(ai_client.generate_content("hello"))

    assert result == "fallback response"
    assert primary.aio.models.generate_content.await_count == 1
    assert fallback.aio.models.generate_content.await_count == 1


def test_generate_content_uses_api_key_first_when_preferred():
    from app.ai import client as ai_client

    api_key_client = MagicMock()
    api_key_client.aio.models.generate_content = AsyncMock(
        return_value=MagicMock(text="api key response")
    )
    vertex_client = MagicMock()

    with (
        patch.object(ai_client, "API_KEY", "test-key"),
        patch.object(ai_client, "PREFER_API_KEY", True),
        patch.object(ai_client, "get_api_key_client", return_value=api_key_client),
        patch.object(ai_client, "get_vertex_client", return_value=vertex_client),
    ):
        result = asyncio.run(ai_client.generate_content("hello"))

    assert result == "api key response"
    vertex_client.aio.models.generate_content.assert_not_called()


def test_generate_content_reports_all_provider_failures():
    from app.ai import client as ai_client

    with (
        patch.object(ai_client, "API_KEY", None),
        patch.object(ai_client, "get_vertex_client", side_effect=RuntimeError("no ADC")),
    ):
        try:
            asyncio.run(ai_client.generate_content("hello"))
        except RuntimeError as exc:
            assert "All Gemini providers failed" in str(exc)
        else:
            raise AssertionError("generate_content should raise when every provider fails")


def _mock_gemini(text: str):
    return patch("app.ai.client.generate_content", new=AsyncMock(return_value=text))


def test_modify_preserves_existing_nodes_edges_and_fields():
    current = sample_arch()
    generated_yaml = """
name: test_system
slo:
  error_rate_max: 0.01
  p99_ms_max: 2000
traffic:
  sources:
    - name: users
      target: lb
      rps: 100
nodes:
  - id: lb
    type: load_balancer
    replicas: 2
    concurrency_per_replica: 1000
  - id: api
    type: service
    replicas: 4
    concurrency_per_replica: 20
    queue_per_replica: 50
    startup:
      requires: [db]
edges:
  - from: lb
    to: api
    timeout_ms: 3000
"""
    with _mock_gemini(f"```yaml\n{generated_yaml}\n```"):
        arch, yaml_out, warnings = asyncio.run(generate_architecture(
            "Give api four replicas but keep everything else",
            mode="modify",
            current_architecture=current,
        ))

    assert arch.node("api").replicas == 4
    assert {n.id for n in arch.nodes} == {"lb", "api", "db"}
    assert {e.key for e in arch.edges} == {("lb", "api"), ("api", "db")}
    assert [s.name for s in arch.traffic.sources] == ["users"]
    assert arch.slo.p99_ms_max == 2000
    assert any("Preserved node 'db'" in w for w in warnings)
    assert "api" in yaml_out


def test_generate_multiple_adversary_scenarios_with_count_and_seed():
    payload = [
        {
            "hypothesis": "DB latency spike causes queue saturation",
            "rationale": "db is a single replica datastore",
            "scenario": {
                "name": "db_slow",
                "description": "Slow the database",
                "duration_s": 200,
                "warmup_s": 20,
                "traffic": {"multiplier": 1.0},
                "events": [{"at_s": 60, "action": "slow", "target": "db", "factor": 5.0}],
            },
        },
        {
            "hypothesis": "Restarting api stalls startup",
            "rationale": "api depends on db at boot",
            "scenario": {
                "name": "api_restart",
                "description": "Restart the API",
                "events": [{"at_s": 80, "action": "restart", "target": "api"}],
            },
        },
    ]
    with _mock_gemini("```json\n" + json.dumps(payload) + "\n```"):
        entries, warnings = asyncio.run(generate_adversary_scenarios(
            sample_arch(), count=2, seed=7, exclude_ids=[]
        ))

    assert len(entries) == 2
    assert [e["scenario_id"].split("_")[1] for e in entries] == ["1", "2"]
    assert len({e["scenario_id"] for e in entries}) == 2
    assert all(e["seed"] == 7 for e in entries)
    assert entries[0]["target_nodes"] == ["db"]
    assert entries[0]["rationale"] == "db is a single replica datastore"
    assert entries[0]["hypothesis"].startswith("DB latency")
    assert "name: db_slow" in entries[0]["yaml"]
    assert warnings == []


def test_generate_multiple_fixes_with_discrete_patches():
    summary = {
        "status": "no_recovery",
        "error_rate": 0.4,
        "p99_ms": 5000,
        "nodes": [{"id": "db", "peak_util": 1.4, "max_queued": 200, "restarts": 0, "errors": {}}],
        "timeline": [{"t": 100, "err_rate": 0.4}],
    }
    payload = [
        {
            "diagnosis": "api has no capacity headroom",
            "root_cause_node": "api",
            "recommended_pattern": "Horizontal Scaling",
            "rationale": "More replicas absorb the spike.",
            "severity": "high",
            "predicted_impact": "Lower p99",
            "patch": {"ops": [{"op": "set", "path": "/nodes/api/replicas", "value": 6}]},
        },
        {
            "diagnosis": "Retries amplify DB load",
            "root_cause_node": "db",
            "recommended_pattern": "Backoff with Jitter",
            "rationale": "Spread retries out.",
            "severity": "medium",
            "predicted_impact": "Faster recovery",
            "patch": {"ops": [
                {"op": "set", "path": "/edges/api->db/retry/backoff", "value": "exponential"},
                {"op": "set", "path": "/edges/api->db/retry/jitter", "value": True},
            ]},
        },
    ]
    with _mock_gemini("```json\n" + json.dumps(payload) + "\n```"):
        fixes = asyncio.run(generate_remediation_fixes(
            sample_arch(), summary, findings=[], count=2
        ))

    assert len(fixes) == 2
    assert all(f["valid"] for f in fixes), [f["validation_errors"] for f in fixes]
    assert fixes[0]["patched_architecture"]["nodes"][1]["replicas"] == 6
    assert fixes[1]["rationale"] == "Spread retries out."
    assert fixes[1]["patch"]["source_fix_id"] == fixes[1]["fix_id"]
    assert len({f["patch"]["base_fingerprint"] for f in fixes}) == 1
    assert all(len(f["patch"]["base_fingerprint"]) == 64 for f in fixes)
    assert "nodes:" in fixes[0]["patched_yaml"]


def test_remediation_falls_back_to_safe_patches_when_gemini_fails():
    summary = {"status": "no_recovery", "error_rate": 0.5, "nodes": []}
    with patch("app.ai.client.generate_content", new=AsyncMock(side_effect=RuntimeError("bad output"))):
        fixes = asyncio.run(generate_remediation_fixes(sample_arch(), summary, count=3))

    assert 1 <= len(fixes) <= 3
    assert all(f["valid"] for f in fixes)
    assert all(f["diagnosis"] and f["patch"]["ops"] for f in fixes)


def test_create_falls_back_to_local_architecture_when_gemini_unavailable():
    with patch("app.ai.client.generate_content", new=AsyncMock(side_effect=RuntimeError("no credentials"))):
        arch, yaml_out, warnings = asyncio.run(generate_architecture(
            "Create a three tier web app with a cache", mode="create"
        ))

    assert len(arch.nodes) >= 4
    assert any(node.type == "cache" for node in arch.nodes)
    assert any(edge.via_cache == "app_cache" for edge in arch.edges)
    assert "name:" in yaml_out
    assert any("fallback" in warning.lower() for warning in warnings)


def test_modify_fallback_adds_node_without_losing_existing_topology():
    current = sample_arch()
    original_nodes = {node.id for node in current.nodes}
    original_edges = {edge.key for edge in current.edges}

    with patch("app.ai.client.generate_content", new=AsyncMock(side_effect=RuntimeError("no credentials"))):
        arch, _, warnings = asyncio.run(generate_architecture(
            "Add a redis cluster for session caching. Keep everything else.",
            mode="modify",
            current_architecture=current,
        ))

    node_ids = {node.id for node in arch.nodes}
    assert original_nodes <= node_ids
    assert "redis_session_cluster" in node_ids
    assert original_edges <= {edge.key for edge in arch.edges}
    assert arch.node("api").replicas == current.node("api").replicas
    assert any("fallback" in warning.lower() for warning in warnings)
