"""Natural language text description to YAML architecture parser powered by Vertex AI."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple
import yaml

from app.ai.client import get_vertex_client, get_model_name
from app.core.model import Architecture

PROMPT_TEMPLATE = """You are a Principal Cloud and Distributed Systems Architect.
Your task is to convert a user's natural language description of their system architecture into a strict, validated YAML specification.

### REFERENCE FORMAT (from ./backend/models/):
Follow the exact structure shown below:

```yaml
name: system_name
slo:
  error_rate_max: 0.01      # max error rate acceptable (e.g. 0.01 = 1%)
  p99_ms_max: 2500          # p99 latency threshold in ms
  window_s: 30              # evaluation window in seconds
  recover_within_s: 300     # recovery time in seconds after incident

traffic:
  sources:
    - name: api_clients
      target: edge_lb       # target node id where traffic enters
      rps: 300              # requests per second
      timeout_ms: 30000

nodes:
  - id: edge_lb
    type: load_balancer     # One of: load_balancer, shard_router, service, cache, datastore, external
    replicas: 2
    concurrency_per_replica: 1500
    queue_per_replica: 200
    latency_ms: { p50: 1, p99: 4 }

  - id: core_service
    type: service
    replicas: 3
    concurrency_per_replica: 25
    queue_per_replica: 80
    latency_ms: { p50: 15, p99: 50 }
    startup:
      time_s: 10
      requires: [primary_db]   # list of node IDs that must be ready before this node boots
    health_check:
      interval_s: 5
      fail_threshold: 0.3
      unhealthy_after: 3

  - id: cache_layer
    type: cache
    replicas: 2
    concurrency_per_replica: 800
    latency_ms: { p50: 2, p99: 6 }
    warm_hit_rate: 0.90
    cold_hit_rate: 0.10
    warmup_s: 120

  - id: primary_db
    type: datastore
    replicas: 1
    concurrency_per_replica: 30
    queue_per_replica: 100
    latency_ms: { p50: 15, p99: 75 }

edges:
  - from: edge_lb
    to: core_service
    timeout_ms: 4000

  - from: core_service
    to: cache_layer
    timeout_ms: 200

  - from: core_service
    to: primary_db
    timeout_ms: 2000
    pool: 30               # connection pool limit
    retry: { attempts: 2, backoff: exponential, base_ms: 50, max_ms: 300, jitter: true }
```

### STRICT RULES:
1. Every edge 'from' and 'to' must reference an existing node ID defined under 'nodes'.
2. The traffic source 'target' must reference an existing node ID (usually a load balancer or gateway).
3. If a node specifies `startup.requires: [node_id]`, that `node_id` must exist under `nodes`.
4. Node types MUST strictly be one of: `load_balancer`, `shard_router`, `service`, `cache`, `datastore`, `external`.
5. Edge retry backoff MUST be one of: `none`, `fixed`, `exponential`.
6. Provide realistic estimates for `replicas`, `concurrency_per_replica`, `latency_ms: {p50, p99}`, and `timeout_ms` based on the user's description.
7. Return ONLY the raw YAML text within a markdown ```yaml ... ``` code block. Do not include conversational preamble.

### USER ARCHITECTURE DESCRIPTION:
__DESCRIPTION__
"""


def clean_yaml_response(text: str) -> str:
    """Extract raw YAML from markdown fences or response text."""
    pattern = r"```(?:ya?ml)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


async def text_to_architecture_yaml(
    description: str,
    override_name: str | None = None
) -> Tuple[Architecture, str]:
    """
    Invokes Vertex AI (Gemini 3.5 Flash) to convert natural language text into a validated Architecture model.
    Returns (Architecture instance, YAML string).
    """
    client = get_vertex_client()
    model = get_model_name()

    prompt = PROMPT_TEMPLATE.replace("__DESCRIPTION__", description)

    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt
    )

    yaml_text = clean_yaml_response(response.text)

    # Parse YAML into Python dictionary
    raw_data = yaml.safe_load(yaml_text)
    if not isinstance(raw_data, dict):
        raise ValueError("Model output failed to parse into a valid YAML dictionary.")

    if override_name:
        raw_data["name"] = override_name

    # Validate against strict Pydantic Architecture schema
    arch = Architecture.model_validate(raw_data)

    # Re-dump normalized clean YAML
    clean_yaml = yaml.dump(raw_data, sort_keys=False)

    return arch, clean_yaml
