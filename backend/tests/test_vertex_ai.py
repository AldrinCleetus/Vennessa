"""Unit tests for Vertex AI integration services."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.model import Architecture, Node, Edge, Traffic, Source, SLO
from app.ai.parser import text_to_architecture_yaml, clean_yaml_response
from app.ai.adversary import generate_adversary_scenario, clean_yaml
from app.ai.remediator import remediate_architecture, clean_json


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

    with patch("app.ai.parser.get_vertex_client") as mock_client_factory:
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

    with patch("app.ai.adversary.get_vertex_client") as mock_client_factory:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
        mock_client_factory.return_value = mock_client

        scenario, yaml_out, hypothesis, targets = asyncio.run(generate_adversary_scenario(arch))
        assert scenario.name == "test_attack"
        assert targets == ["db"]
        assert "db" in yaml_out
