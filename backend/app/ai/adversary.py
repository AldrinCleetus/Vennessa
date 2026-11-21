"""AI Adversary: Generates stress and failure injection scenarios to stress-test architectures."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Tuple

import yaml

from app.ai import client as ai_client
from app.core.analyze import analyze
from app.core.model import Architecture
from app.core.scenario import Scenario

ADVERSARY_PROMPT_TEMPLATE = """You are a Chaos Engineering and Site Reliability Engineering (SRE) Adversary.
Your mission is to find the weakest links in the following distributed system architecture and generate an adversarial stress scenario to prove the failure mode.

### ARCHITECTURE MODEL:
```yaml
__ARCHITECTURE_YAML__
```

### KNOWN NODES IN THIS ARCHITECTURE:
__NODE_IDS__

### ATTACK SCENARIO REFERENCE FORMAT:
Output an adversarial scenario in this strict YAML format:

```yaml
name: adversary_attack_name
description: "Detailed description of why this attack breaks the system (e.g., cascading retry storm when database latency increases by 5x during 3x traffic surge)."
duration_s: 300
warmup_s: 30
seed: 42
traffic:
  multiplier: 1.0
  ramp:
    - at_s: 60
      multiplier: 3.0
      ramp_s: 15
events:
  - at_s: 90
    action: slow          # One of: kill, restore, restart, slow, rate_limit
    target: <NODE_ID>     # MUST be one of the known node IDs listed above!
    factor: 5.0           # For 'slow': multiply latency by this factor
    duration_s: 60
  - at_s: 160
    action: restart
    target: <NODE_ID>
```

### RULES:
1. Every event 'target' MUST be chosen from the known node IDs: __NODE_IDS__.
2. Duration should typically be 180 to 300 seconds, with warmup_s around 20-30 seconds.
3. Target vulnerable points like:
   - High dependency nodes or databases without read replicas
   - Services with retries that will cause retry amplification
   - Nodes without circuit breakers
4. Return ONLY the YAML inside a ```yaml ... ``` code block.
"""

MULTI_ADVERSARY_PROMPT_TEMPLATE = """You are a Chaos Engineering and Site Reliability Engineering (SRE) Adversary.
Generate __COUNT__ DISTINCT adversarial stress scenarios for the architecture below. Each scenario must test a different failure mode or target.

### ARCHITECTURE MODEL:
```yaml
__ARCHITECTURE_YAML__
```

### KNOWN NODE IDs:
__NODE_IDS__

### EXCLUDED SCENARIO IDS / NAMES (do not repeat these):
__EXCLUDE_IDS__

### REQUESTED SEED (use for every scenario unless a different seed is useful):
__SEED__

### STATIC FINDINGS FROM ANALYSIS (these drive the scenarios):
__STATIC_FINDINGS__

Derive attacks from the findings, not from templates. In particular:
- retry_amplification / retry_no_backoff -> a latency spike ("slow") on the
  dependency where retries LAND, at 3-5x for 60-120s, to trigger a retry storm.
- timeout_inversion -> a latency spike on the downstream node that makes the
  caller's budget expire while the callee keeps working.
- spof / over_capacity / low_headroom -> loss of the single replica.
- rate_limit findings -> a rate_limit event at or above the configured limit.

### OUTPUT FORMAT
Return ONLY a JSON array with __COUNT__ entries. Each entry has this shape:
{
  "hypothesis": "What you expect to break and why.",
  "rationale": "Why this attack targets a real weakness in this architecture.",
  "scenario": {
    "name": "unique_attack_name",
    "description": "Detailed description of the failure mode.",
    "duration_s": 300,
    "warmup_s": 30,
    "seed": 42,
    "traffic": {"multiplier": 1.0, "ramp": [{"at_s": 60, "multiplier": 3.0, "ramp_s": 15}]},
    "events": [
      {"at_s": 90, "action": "slow", "target": "<NODE_ID>", "factor": 5.0, "duration_s": 60},
      {"at_s": 160, "action": "restart", "target": "<NODE_ID>"}
    ]
  }
}

### RULES
1. Every event target MUST be one of the known node IDs; valid actions are kill, restore, restart, slow, rate_limit.
2. duration_s should be 180-300, warmup_s 20-30 (or omit them to use defaults).
3. Vary targets and mechanisms across the __COUNT__ scenarios (latency, restarts, traffic surge, rate limits).
4. Return ONLY the JSON array, with no conversational text.
"""


def clean_yaml(text: str) -> str:
    pattern = r"```(?:ya?ml|json)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


def _parse_structured(text: str) -> Any:
    """Parse JSON (preferred) or YAML from a model response."""
    stripped = clean_yaml(text)
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return yaml.safe_load(stripped)


def _scenario_id(
    name: str,
    targets: List[str],
    seed: int,
    used: set[str],
    exclude_ids: set[str],
) -> str:
    digest = hashlib.sha256(f"{name}|{','.join(sorted(set(targets)))}|{seed}".encode()).hexdigest()[:8]
    base = f"ai_{len(used) + 1}_{digest}"
    candidate = base
    suffix = 2
    while candidate in used or candidate in exclude_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _fallback_scenarios(arch: Architecture, count: int, seed: int) -> list[dict[str, Any]]:
    """Deterministic last resort so the UI always has something to click."""
    ids = [n.id for n in arch.nodes]
    if not ids:
        return []
    findings = analyze(arch)
    risky = [f.target for f in findings if f.rule in {"spof", "over_capacity", "low_headroom"}]
    targets = list(dict.fromkeys(risky + ids))
    latency_targets = [
        f.target for f in findings
        if f.rule in {"retry_amplification", "retry_no_backoff", "timeout_inversion"}
        and arch.has_node(str(f.target))
    ]
    scenarios: list[dict[str, Any]] = []
    for i in range(min(count, len(targets))):
        target = targets[i]
        slow = target in latency_targets or (i == 0 and bool(latency_targets))
        if slow and latency_targets:
            target = latency_targets[0] if i == 0 else target
            sc = Scenario.model_validate({
                "name": f"ai_fallback_{i + 1}",
                "description": f"Latency spike on {target} to expose retry amplification and timeout inversion.",
                "duration_s": 300.0,
                "warmup_s": 30.0,
                "seed": seed,
                "events": [{"at_s": 90 + i * 10, "action": "slow", "target": target,
                            "factor": 5.0, "duration_s": 90.0}],
            })
            hypothesis = (f"Dependency latency at {target} amplifies nested retries and exhausts "
                          "upstream queues; a correct retry/timeout fix should stop the cascade.")
        else:
            sc = Scenario.model_validate({
                "name": f"ai_fallback_{i + 1}",
                "description": f"Inject a failure into {target} to observe cascading impact.",
                "duration_s": 300.0,
                "warmup_s": 30.0,
                "seed": seed,
                "events": [{"at_s": 90 + i * 10, "action": "restart", "target": target}],
            })
            hypothesis = f"Restarting {target} exposes startup and failover weaknesses."
        scenarios.append({
            "scenario_id": f"ai_{i + 1}_fallback",
            "scenario": sc.model_dump(),
            "yaml": yaml.dump(sc.model_dump(), sort_keys=False),
            "hypothesis": hypothesis,
            "rationale": "Generated from static analysis because Gemini returned no usable scenarios.",
            "target_nodes": [target],
            "seed": seed,
            "source": "fallback",
        })
    return scenarios


async def generate_adversary_scenarios(
    arch: Architecture,
    count: int = 3,
    seed: int | None = None,
    exclude_ids: List[str] | None = None,
    include_analysis: bool = True,
) -> Tuple[List[dict[str, Any]], List[str]]:
    """Generate multiple distinct, validated adversarial scenarios.

    Returns (scenario entries, warnings). Each entry contains scenario_id,
    scenario dict, YAML, hypothesis, rationale, target nodes and seed.
    """
    count = max(1, min(int(count or 3), 20))
    node_ids = [n.id for n in arch.nodes]
    node_set = set(node_ids)
    exclude = set(exclude_ids or [])
    warnings: List[str] = []

    findings = analyze(arch) if include_analysis else []
    findings_text = (
        yaml.dump(
            [{"rule": f.rule, "severity": f.severity, "target": f.target, "message": f.message}
             for f in findings],
            sort_keys=False,
        )
        if findings else "not included"
    )
    prompt = (
        MULTI_ADVERSARY_PROMPT_TEMPLATE
        .replace("__ARCHITECTURE_YAML__", yaml.dump(arch.model_dump(by_alias=True), sort_keys=False))
        .replace("__NODE_IDS__", ", ".join(node_ids))
        .replace("__EXCLUDE_IDS__", ", ".join(sorted(exclude)) or "none")
        .replace("__SEED__", str(seed if seed is not None else 42))
        .replace("__STATIC_FINDINGS__", findings_text)
        .replace("__COUNT__", str(count))
    )

    try:
        payload = _parse_structured(await ai_client.generate_content(
            prompt, temperature=0.3, json_mode=True
        ))
    except Exception as exc:  # malformed output should not break the whole UI
        warnings.append(f"Gemini scenario generation failed ({type(exc).__name__}); using deterministic fallback.")
        payload = None

    if isinstance(payload, dict):
        payload = payload.get("scenarios") or payload.get("scenario") or [payload]
    if not isinstance(payload, list):
        warnings.append("Gemini returned an unexpected scenario payload; using deterministic fallback.")
        payload = []

    allowed = set(Scenario.model_fields)
    used_ids: set[str] = set()
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(payload):
        if isinstance(raw, str):
            try:
                raw = yaml.safe_load(raw)
            except yaml.YAMLError:
                raw = None
        if not isinstance(raw, dict):
            warnings.append(f"Skipped scenario {index + 1}: expected an object.")
            continue
        candidate = raw.get("scenario") if isinstance(raw.get("scenario"), dict) else raw
        data = {k: v for k, v in candidate.items() if k in allowed}
        hypothesis = raw.get("hypothesis") or candidate.get("hypothesis") or data.get("description") or ""
        rationale = raw.get("rationale") or candidate.get("rationale") or ""
        data.setdefault("name", f"ai_scenario_{index + 1}")
        if str(data["name"]) in exclude:
            warnings.append(f"Skipped excluded scenario '{data['name']}'.")
            continue
        data.setdefault("duration_s", 300.0)
        data.setdefault("warmup_s", 30.0)
        data["seed"] = seed if seed is not None else data.get("seed", 42)

        filtered = []
        for ev in data.get("events") or []:
            target = ev.get("target") if isinstance(ev, dict) else None
            if target in node_set:
                filtered.append(ev)
            else:
                warnings.append(f"Dropped event targeting unknown node {target!r} in '{data['name']}'.")
        data["events"] = filtered

        try:
            scenario = Scenario.model_validate(data)
        except Exception as exc:
            warnings.append(f"Skipped invalid scenario '{data.get('name')}': {exc}")
            continue
        scenario_dict = scenario.model_dump()
        targets = list(dict.fromkeys(e.target for e in scenario.events))
        scenario_id = _scenario_id(str(scenario.name), targets, scenario.seed, used_ids, exclude)
        entries.append({
            "scenario_id": scenario_id,
            "scenario": scenario_dict,
            "yaml": yaml.dump(scenario_dict, sort_keys=False),
            "hypothesis": hypothesis or scenario.description,
            "rationale": rationale,
            "target_nodes": targets,
            "seed": scenario.seed,
            "source": "gemini",
        })
        if len(entries) >= count:
            break

    if not entries:
        entries = _fallback_scenarios(arch, count, seed if seed is not None else 42)
        warnings.append(
            "No Gemini scenarios were valid; generated deterministic fallback scenarios "
            "derived from static analysis (scenario_source: fallback)."
        )
    elif isinstance(payload, list) and len(entries) < len(payload):
        warnings.append(
            f"Gemini returned {len(payload)} scenarios but only {len(entries)} were valid; "
            "the rest were dropped (scenario_source: gemini)."
        )
    return entries, warnings


async def generate_adversary_scenario(
    arch: Architecture
) -> Tuple[Scenario, str, str, List[str]]:
    """
    Compatibility wrapper: generates one targeted failure scenario for the architecture.
    Returns (Scenario object, scenario YAML string, hypothesis description, target nodes).
    """
    entries, _ = await generate_adversary_scenarios(arch, count=1)
    entry = entries[0]
    return (
        Scenario.model_validate(entry["scenario"]),
        entry["yaml"],
        entry["hypothesis"],
        entry["target_nodes"],
    )
