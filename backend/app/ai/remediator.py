"""AI Remediator: Analyzes simulation failures and produces discrete architectural fixes."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Tuple

import yaml

from app.ai import client as ai_client
from app.core.model import Architecture
from app.core.patch import (
    PatchConflictError,
    PatchValidationError,
    apply_architecture_patch,
    architecture_fingerprint,
)

SEVERITIES = {"critical", "high", "medium", "low"}

REMEDIATOR_PROMPT_TEMPLATE = """You are a Principal Cloud Resilience Engineer.
The architecture below failed its SLOs under a stress simulation. Propose multiple ALTERNATIVE fixes.

### CURRENT ARCHITECTURE YAML:
```yaml
__ARCHITECTURE_YAML__
```

### RICH SIMULATION SUMMARY & STATIC FINDINGS:
```json
__SIMULATION_SUMMARY_JSON__
```

### OUTPUT FORMAT
Return ONLY a JSON ARRAY of __COUNT__ alternative fix objects with this exact shape:
```json
[
  {
    "diagnosis": "Why the architecture failed under stress.",
    "root_cause_node": "node id (or edge a->b) with the root cause",
    "recommended_pattern": "e.g. Bounded Retry with Jitter + Health Checks",
    "rationale": "Why this specific change stops the outage.",
    "severity": "critical|high|medium|low",
    "predicted_impact": "Expected effect on error rate / p99 / recovery.",
    "patch": {
      "base_fingerprint": "__BASE_FINGERPRINT__",
      "ops": [
        {"op": "set", "path": "/nodes/<id>/replicas", "value": 4}
      ]
    }
  }
]
```

### VALID PATCH OPS
- op is one of: set, add, remove, replace.
- `set` deep-merges dict values and replaces scalars/lists. `replace` replaces the whole value.
- `add` appends to a list (or adds a full entity at an identity path). `remove` deletes and cleans references.

### EXACT VALID PATH EXAMPLES
- /nodes/<id>/replicas
- /nodes/<id>/concurrency_per_replica
- /nodes/<id>/health_check/interval_s
- /nodes/<id>/read_replicas (list)
- /edges/<from>-><to>/retry/attempts
- /edges/<from>-><to>/retry/jitter
- /edges/<from>-><to>/retry/backoff
- /edges/<from>-><to>/timeout_ms
- /edges/<from>-><to>/pool
- /traffic/sources/<name>/rps
- /slo/error_rate_max, /slo/p99_ms_max
- `add` to /nodes, /edges, /traffic/sources with a full object as `value`
- `remove` /nodes/<id>, /edges/<from>-><to>, /traffic/sources/<name>

### RULES
1. Return discrete patch operations, NOT a full replacement architecture YAML.
2. Use only node ids / edge keys / traffic source names that exist in the architecture above.
3. Make the __COUNT__ fixes genuinely different alternatives (e.g. scale out, tune retries, shed load, health checks).
4. Return ONLY the JSON array, no conversational text.
"""


def clean_json(text: str) -> str:
    pattern = r"```(?:json|ya?ml)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


def _parse_payload(text: str) -> Any:
    stripped = clean_json(text)
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return yaml.safe_load(stripped)


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _severity(value: Any, summary: Dict[str, Any]) -> str:
    if isinstance(value, str) and value.lower() in SEVERITIES:
        return value.lower()
    if summary.get("status") in {"no_recovery", "slow_recovery"} or (summary.get("error_rate") or 0) > 0.2:
        return "critical"
    return "high"


def _simulation_context(
    simulation_summary: Dict[str, Any], findings: List[Dict[str, Any]] | None
) -> Dict[str, Any]:
    """Keep the prompt rich but bounded (the frontend sends the full /simulate result)."""
    keys = (
        "status", "passed", "error_rate", "p99_ms", "p50_ms", "requests",
        "breach_seconds", "first_breach_s", "last_breach_s", "recovery_s",
        "skipped_targets", "cascade", "client_errors", "node_errors", "nodes",
        "sim_events", "timeline", "slo_breached",
    )
    context = {k: simulation_summary[k] for k in keys if k in simulation_summary}
    if isinstance(context.get("timeline"), list):
        context["timeline"] = context["timeline"][:30]
    if isinstance(context.get("cascade"), list):
        context["cascade"] = context["cascade"][:20]
    context["static_findings"] = findings or simulation_summary.get("findings") or []
    return context


def _root_cause(
    arch: Architecture, summary: Dict[str, Any], findings: List[Dict[str, Any]]
) -> str:
    for finding in findings:
        if finding.get("rule") in {"spof", "over_capacity", "low_headroom", "cold_cache_stampede"}:
            return str(finding.get("target") or arch.nodes[0].id)
    for finding in findings:
        target = str(finding.get("target") or "")
        if "->" in target:
            return target.split("->", 1)[1]
    nodes = summary.get("nodes") or []
    if isinstance(nodes, list):
        saturated = [n for n in nodes if isinstance(n, dict) and (n.get("peak_util") or 0) > 0.8]
        if saturated:
            return str(saturated[0].get("id") or arch.nodes[0].id)
    return arch.nodes[0].id if arch.nodes else "unknown"


def _make_fix(
    arch: Architecture,
    base_fingerprint: str,
    fix_id: str,
    candidate: Dict[str, Any],
    ops: List[Dict[str, Any]],
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    supplied = candidate.get("patch") if isinstance(candidate.get("patch"), dict) else {}
    patch = {
        "base_fingerprint": supplied.get("base_fingerprint") or base_fingerprint,
        "ops": ops,
        "source_fix_id": fix_id,
    }
    errors: List[str] = []
    patched_arch = arch
    patched_yaml = yaml.dump(arch.model_dump(by_alias=True), sort_keys=False)
    valid = False
    try:
        patched_arch, _, _ = apply_architecture_patch(arch, patch)
        patched_yaml = yaml.dump(patched_arch.model_dump(by_alias=True), sort_keys=False)
        valid = True
    except (PatchConflictError, PatchValidationError) as exc:
        errors.append(str(exc))
    except Exception as exc:
        errors.append(f"Unexpected patch error: {exc}")

    root_cause = str(candidate.get("root_cause_node") or _root_cause(arch, summary, []))
    return {
        "fix_id": fix_id,
        "diagnosis": candidate.get("diagnosis") or "Bottleneck identified under simulated stress load.",
        "root_cause_node": root_cause,
        "recommended_pattern": candidate.get("recommended_pattern") or "Capacity and Resilience Tuning",
        "rationale": candidate.get("rationale") or "",
        "severity": _severity(candidate.get("severity"), summary),
        "predicted_impact": candidate.get("predicted_impact") or "Reduce errors and shorten recovery after the injected failure.",
        "patch": {
            "base_fingerprint": patch["base_fingerprint"],
            "ops": ops,
            "source_fix_id": fix_id,
        },
        "patched_architecture": patched_arch.model_dump(by_alias=True),
        "patched_yaml": patched_yaml,
        "valid": valid,
        "validation_errors": errors,
    }


def _fallback_fixes(
    arch: Architecture,
    base_fingerprint: str,
    summary: Dict[str, Any],
    findings: List[Dict[str, Any]],
    count: int,
) -> List[Dict[str, Any]]:
    """Derive safe patch ops from findings + saturated nodes when Gemini is unusable."""
    if not arch.nodes:
        return []
    root = _root_cause(arch, summary, findings)
    if not arch.has_node(root):
        root = arch.nodes[0].id

    specs: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []

    overload = next(
        (f for f in findings if f.get("rule") in {"over_capacity", "low_headroom", "cold_cache_stampede"}),
        None,
    )
    overload_id = str(overload.get("target")) if overload and arch.has_node(str(overload.get("target"))) else root
    replicas = arch.node(overload_id).replicas
    specs.append((
        {
            "diagnosis": f"{overload_id} saturates under load; its queue fills and requests are rejected.",
            "root_cause_node": overload_id,
            "recommended_pattern": "Horizontal Scaling / Capacity Headroom",
            "rationale": "Doubling replicas gives the node enough slots to absorb the simulated spike.",
            "severity": "high" if not overload else overload.get("severity", "high"),
            "predicted_impact": "Lower error rate and p99 during traffic surges.",
        },
        [{"op": "set", "path": f"/nodes/{overload_id}/replicas", "value": max(2, replicas * 2)}],
    ))

    retry_target = next(
        (f.get("target") for f in findings if "retry" in str(f.get("rule")) and "->" in str(f.get("target"))),
        None,
    )
    if not retry_target and arch.edges:
        retry_target = next(
            (f"{e.from_}->{e.to}" for e in arch.edges if e.to == root),
            f"{arch.edges[0].from_}->{arch.edges[0].to}",
        )
    if retry_target:
        edge = next((e for e in arch.edges if f"{e.from_}->{e.to}" == retry_target), None)
        specs.append((
            {
                "diagnosis": f"Retries on {retry_target} amplify load on an already struggling dependency.",
                "root_cause_node": root,
                "recommended_pattern": "Bounded Retry with Exponential Backoff and Jitter",
                "rationale": "Backoff with jitter spreads retries out instead of hammering the failing dependency in lockstep.",
                "severity": "medium",
                "predicted_impact": "Fewer duplicate requests and faster recovery after a restart.",
            },
            [
                {"op": "set", "path": f"/edges/{retry_target}/retry/backoff", "value": "exponential"},
                {"op": "set", "path": f"/edges/{retry_target}/retry/jitter", "value": True},
                {"op": "set", "path": f"/edges/{retry_target}/retry/attempts",
                 "value": max(2, edge.retry.attempts) if edge else 2},
            ],
        ))

    health_target = next(
        (e.to for e in arch.edges if arch.node(e.from_).type == "load_balancer"),
        root,
    )
    if health_target and arch.has_node(health_target):
        specs.append((
            {
                "diagnosis": f"Broken replicas of {health_target} keep receiving traffic after a failure.",
                "root_cause_node": health_target,
                "recommended_pattern": "Health-Check-Driven Failover",
                "rationale": "A shallow health check lets the load balancer pull unhealthy replicas quickly.",
                "severity": "medium",
                "predicted_impact": "Fewer requests routed to dead replicas during recovery.",
            },
            [{
                "op": "set",
                "path": f"/nodes/{health_target}/health_check",
                "value": {"interval_s": 5, "fail_threshold": 0.3, "unhealthy_after": 3, "deep": False},
            }],
        ))

    fixes: List[Dict[str, Any]] = []
    for index, (candidate, ops) in enumerate(specs[:count]):
        fix_id = f"fix_{index + 1}_{_short_hash(json.dumps(ops, sort_keys=True, default=str))}"
        fixes.append(_make_fix(arch, base_fingerprint, fix_id, candidate, ops, summary))
    return fixes


async def generate_remediation_fixes(
    arch: Architecture,
    simulation_summary: Dict[str, Any],
    findings: List[Dict[str, Any]] | None = None,
    count: int = 3,
    base_fingerprint: str | None = None,
) -> List[Dict[str, Any]]:
    """Generate multiple discrete, validated fix candidates.

    Invalid candidates are returned with valid=false instead of aborting the
    whole batch. If Gemini output is malformed, deterministic safe fixes are
    generated from static findings and simulation metrics.
    """
    count = max(1, min(int(count or 3), 10))
    fingerprint = base_fingerprint or architecture_fingerprint(arch)
    summary = _simulation_context(simulation_summary or {}, findings)
    prompt = (
        REMEDIATOR_PROMPT_TEMPLATE
        .replace("__ARCHITECTURE_YAML__", yaml.dump(arch.model_dump(by_alias=True), sort_keys=False))
        .replace("__SIMULATION_SUMMARY_JSON__", json.dumps(summary, indent=2, default=str))
        .replace("__BASE_FINGERPRINT__", fingerprint)
        .replace("__COUNT__", str(count))
    )

    try:
        payload = _parse_payload(await ai_client.generate_content(prompt))
    except Exception:
        payload = None

    if isinstance(payload, dict):
        payload = payload.get("fixes") or [payload]
    if not isinstance(payload, list):
        payload = []

    fixes: List[Dict[str, Any]] = []
    for index, candidate in enumerate(payload):
        if not isinstance(candidate, dict):
            continue
        patch = candidate.get("patch") if isinstance(candidate.get("patch"), dict) else {}
        ops = patch.get("ops") or candidate.get("ops")
        if not isinstance(ops, list) or not ops:
            continue
        seed_text = json.dumps(
            [candidate.get("diagnosis"), candidate.get("root_cause_node"), ops],
            sort_keys=True,
            default=str,
        )
        fix_id = f"fix_{len(fixes) + 1}_{_short_hash(seed_text)}"
        fixes.append(_make_fix(arch, fingerprint, fix_id, candidate, ops, summary))
        if len(fixes) >= count:
            break

    if not fixes:
        fixes = _fallback_fixes(arch, fingerprint, summary, findings or [], count)
    return fixes


async def remediate_architecture(
    arch: Architecture,
    simulation_summary: Dict[str, Any]
) -> Tuple[str, str, str, Architecture, str]:
    """Compatibility wrapper returning the first fix as the old 5-tuple."""
    fixes = await generate_remediation_fixes(arch, simulation_summary, count=1)
    fix = fixes[0]
    return (
        fix["diagnosis"],
        fix["root_cause_node"],
        fix["recommended_pattern"],
        Architecture.model_validate(fix["patched_architecture"]),
        fix["patched_yaml"],
    )
