"""Natural language text description to YAML architecture parser powered by Vertex AI."""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Tuple

import yaml

from app.ai import client as ai_client
from app.core.model import Architecture
from app.core.reconcile import reconcile_architecture

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

MODIFY_INSTRUCTIONS = """### MODE: MODIFY AN EXISTING ARCHITECTURE
Preserve every field/node/edge/traffic source/SLO not explicitly changed by the user. Return the COMPLETE updated YAML.
Include ALL original nodes, edges, traffic sources and settings, even ones the user does not mention.
If the user asks to remove something, omit it; otherwise keep it exactly as-is.

### CURRENT ARCHITECTURE (complete YAML):
```yaml
__CURRENT_ARCHITECTURE_YAML__
```

"""


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    return slug or "generated_architecture"


def _has_word(word: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text))


def _unique_id(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    return f"{base}_{index}"


def _explicit_id(description: str, default: str) -> str:
    match = re.search(
        r"(?:called|named|with\s+id|id\s*[:=])\s+[`'\"]([A-Za-z0-9_-]+)[`'\"]",
        description,
        re.IGNORECASE,
    )
    return match.group(1) if match else default


def _requested_replicas(description: str, current: int) -> int | None:
    lowered = description.lower()
    patterns = (
        r"replicas?\s*(?:to|=|:)\s*(\d+)",
        r"(\d+)\s+replicas?",
        r"scale[^\d]{0,40}(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return max(1, int(match.group(1)))
    if re.search(r"\b(?:double|twice)\b", lowered):
        return max(1, current * 2)
    return None


def _fallback_create_architecture(description: str, override_name: str | None) -> Dict[str, Any]:
    """A useful three-tier baseline when no Gemini provider is reachable."""
    lowered = description.lower()
    name = _slugify(override_name or description[:80])
    nodes: List[Dict[str, Any]] = [
        {
            "id": "edge_lb",
            "type": "load_balancer",
            "replicas": 2,
            "concurrency_per_replica": 1200,
            "queue_per_replica": 200,
            "latency_ms": {"p50": 1, "p99": 4},
        },
        {
            "id": "app_service",
            "type": "service",
            "replicas": 3,
            "concurrency_per_replica": 30,
            "queue_per_replica": 80,
            "latency_ms": {"p50": 15, "p99": 60},
            "startup": {"time_s": 10, "requires": ["primary_db"]},
            "health_check": {"interval_s": 5, "fail_threshold": 0.3, "unhealthy_after": 3},
        },
        {
            "id": "primary_db",
            "type": "datastore",
            "replicas": 1,
            "concurrency_per_replica": 40,
            "queue_per_replica": 120,
            "latency_ms": {"p50": 10, "p99": 75},
        },
    ]
    edges: List[Dict[str, Any]] = [
        {"from": "edge_lb", "to": "app_service", "timeout_ms": 4000},
        {"from": "app_service", "to": "primary_db", "timeout_ms": 2000},
    ]

    if re.search(r"\b(?:cache|redis|memcached|session\s+store)\b", lowered):
        nodes.append({
            "id": "app_cache",
            "type": "cache",
            "replicas": 2,
            "concurrency_per_replica": 800,
            "queue_per_replica": 200,
            "latency_ms": {"p50": 2, "p99": 6},
            "warm_hit_rate": 0.9,
            "cold_hit_rate": 0.1,
            "warmup_s": 60,
        })
        edges.append({"from": "app_service", "to": "app_cache", "timeout_ms": 200})
        edges[-2]["via_cache"] = "app_cache"

    if re.search(r"\b(?:queue|kafka|rabbitmq|sqs|pub\s*/?\s*sub)\b", lowered):
        nodes.append({
            "id": "message_queue",
            "type": "service",
            "description": "Asynchronous message queue",
            "replicas": 2,
            "concurrency_per_replica": 200,
            "latency_ms": {"p50": 3, "p99": 20},
        })
        edges.append({"from": "app_service", "to": "message_queue", "timeout_ms": 1000})

    if re.search(r"\b(?:payment|stripe|external api|third.party)\b", lowered):
        nodes.append({
            "id": "external_api",
            "type": "external",
            "replicas": 1,
            "concurrency_per_replica": 100,
            "rate_limit_rps": 200,
            "latency_ms": {"p50": 80, "p99": 400},
        })
        edges.append({"from": "app_service", "to": "external_api", "timeout_ms": 1500})

    return {
        "name": name,
        "description": description.strip(),
        "slo": {"error_rate_max": 0.01, "p99_ms_max": 2500, "window_s": 30},
        "traffic": {"sources": [{"name": "web_clients", "target": "edge_lb", "rps": 250}]},
        "nodes": nodes,
        "edges": edges,
    }


def _fallback_modify_architecture(
    current: Architecture, description: str
) -> Tuple[Dict[str, Any], List[str]]:
    """Make conservative, explicit edits while preserving the current model."""
    data = copy.deepcopy(current.model_dump(by_alias=True))
    lowered = description.lower()
    messages: List[str] = []
    ids = [str(node.get("id")) for node in data.get("nodes", [])]
    mentioned = [node_id for node_id in ids if _has_word(node_id, lowered)]

    if re.search(r"\breplicas?\b|\bscale\b|\bcapacity\b", lowered) and mentioned:
        target_id = mentioned[0]
        target = next(n for n in data["nodes"] if str(n.get("id")) == target_id)
        wanted = _requested_replicas(description, int(target.get("replicas") or 1))
        if wanted is not None:
            target["replicas"] = wanted
            messages.append(f"Local fallback set {target_id} replicas to {wanted}.")

    wants_add = bool(re.search(r"\b(?:add|create|include|introduce|deploy)\b", lowered))
    is_replica_only = wants_add and bool(re.search(r"\breplicas?\b", lowered)) and mentioned
    if wants_add and not is_replica_only:
        if re.search(r"\b(?:redis|cache|memcached|session\s+store)\b", lowered):
            default_id, node_type = "redis_session_cluster", "cache"
        elif re.search(r"\b(?:database|datastore|postgres|mysql|mongo|db)\b", lowered):
            default_id, node_type = "new_datastore", "datastore"
        elif re.search(r"\b(?:queue|kafka|rabbitmq|sqs)\b", lowered):
            default_id, node_type = "message_queue", "service"
        else:
            default_id, node_type = "new_service", "service"

        new_id = _unique_id(_explicit_id(description, default_id), set(ids))
        if node_type == "cache":
            node: Dict[str, Any] = {
                "id": new_id,
                "type": "cache",
                "replicas": 2,
                "concurrency_per_replica": 800,
                "queue_per_replica": 200,
                "latency_ms": {"p50": 2, "p99": 6},
                "warm_hit_rate": 0.9,
                "cold_hit_rate": 0.1,
                "warmup_s": 60,
            }
        elif node_type == "datastore":
            node = {
                "id": new_id,
                "type": "datastore",
                "replicas": 2,
                "concurrency_per_replica": 50,
                "queue_per_replica": 120,
                "latency_ms": {"p50": 10, "p99": 75},
            }
        else:
            node = {
                "id": new_id,
                "type": "service",
                "replicas": 2,
                "concurrency_per_replica": 40,
                "queue_per_replica": 80,
                "latency_ms": {"p50": 15, "p99": 60},
            }

        data["nodes"].append(node)
        sources = [
            str(n.get("id")) for n in data["nodes"]
            if n.get("type") == "service" and n.get("id") != new_id
        ]
        source_id = sources[-1] if sources else (ids[0] if ids else None)
        if source_id and source_id != new_id:
            edge = {"from": source_id, "to": new_id, "timeout_ms": 200 if node_type == "cache" else 2000}
            existing = {(e.get("from"), e.get("to")) for e in data.get("edges", [])}
            if (source_id, new_id) not in existing:
                data.setdefault("edges", []).append(edge)
        messages.append(f"Local fallback added {node_type} '{new_id}'.")

    reconciled, reconcile_warnings = reconcile_architecture(current, data, description)
    # These warnings refer to Gemini output; the fallback already merges the full model.
    reconcile_warnings = [w for w in reconcile_warnings if "omitted by Gemini" not in w]
    return reconciled.model_dump(by_alias=True), messages + reconcile_warnings


def clean_yaml_response(text: str) -> str:
    """Extract raw YAML from markdown fences or response text."""
    pattern = r"```(?:ya?ml)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


def _build_prompt(
    description: str,
    mode: str,
    current_architecture: Architecture | None,
) -> str:
    prompt = PROMPT_TEMPLATE
    if mode == "modify" and current_architecture is not None:
        current_yaml = yaml.dump(current_architecture.model_dump(by_alias=True), sort_keys=False)
        modify_section = MODIFY_INSTRUCTIONS.replace("__CURRENT_ARCHITECTURE_YAML__", current_yaml)
        prompt = prompt.replace(
            "### USER ARCHITECTURE DESCRIPTION:",
            modify_section + "### USER ARCHITECTURE DESCRIPTION:",
        )
    return prompt.replace("__DESCRIPTION__", description)


async def _generate_raw_architecture(
    description: str,
    override_name: str | None,
    mode: str,
    current_architecture: Architecture | None,
) -> Dict[str, Any]:
    prompt = _build_prompt(description, mode, current_architecture)
    response_text = await ai_client.generate_content(prompt)
    raw_data = yaml.safe_load(clean_yaml_response(response_text))
    if not isinstance(raw_data, dict):
        raise ValueError("Model output failed to parse into a valid YAML dictionary.")
    if override_name:
        raw_data["name"] = override_name
    return raw_data


async def text_to_architecture_yaml(
    description: str,
    override_name: str | None = None
) -> Tuple[Architecture, str]:
    """
    Invokes Gemini (Vertex AI, or the API-key fallback) to convert natural language text into a validated Architecture model.
    Returns (Architecture instance, YAML string).
    """
    raw_data = await _generate_raw_architecture(description, override_name, "create", None)
    arch = Architecture.model_validate(raw_data)
    clean_yaml = yaml.dump(raw_data, sort_keys=False)
    return arch, clean_yaml


async def generate_architecture(
    description: str,
    override_name: str | None = None,
    mode: str = "create",
    current_architecture: Architecture | None = None,
) -> Tuple[Architecture, str, List[str]]:
    """Create a new architecture, or safely modify the current one.

    Returns (validated architecture, normalized YAML, warnings). In modify mode
    the generated output is reconciled against the current architecture so
    unrelated nodes/edges/fields are guaranteed to survive.
    """
    if mode not in {"create", "modify"}:
        raise ValueError(f"Unsupported text-to-model mode: {mode!r}")
    if mode == "modify" and current_architecture is None:
        raise ValueError("current_architecture is required in modify mode")

    warnings: List[str] = []
    fallback_used = False
    try:
        raw = await _generate_raw_architecture(description, override_name, mode, current_architecture)
    except Exception as exc:
        # A local fallback keeps the whole workflow usable when ADC/API keys are
        # unavailable. The warning makes it explicit that Gemini was not used.
        fallback_used = True
        warnings.append(f"Gemini unavailable ({type(exc).__name__}); used deterministic local fallback.")
        if mode == "create":
            raw = _fallback_create_architecture(description, override_name)
        else:
            raw, fallback_warnings = _fallback_modify_architecture(current_architecture, description)
            warnings.extend(fallback_warnings)

    if mode == "create":
        arch = Architecture.model_validate(raw)
        return arch, yaml.dump(arch.model_dump(by_alias=True), sort_keys=False), warnings

    if fallback_used:
        arch = Architecture.model_validate(raw)
        return arch, yaml.dump(arch.model_dump(by_alias=True), sort_keys=False), list(dict.fromkeys(warnings))

    # Gemini may return a partial/invalid document (e.g. a changed node that
    # references a node it omitted). Reconciling against the current model first
    # restores the missing entities before strict validation.
    arch, reconcile_warnings = reconcile_architecture(current_architecture, raw, description)
    warnings.append("Reconciled generated architecture against the current architecture.")
    clean_yaml = yaml.dump(arch.model_dump(by_alias=True), sort_keys=False)
    return arch, clean_yaml, warnings + reconcile_warnings
