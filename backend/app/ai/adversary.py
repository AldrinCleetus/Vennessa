"""AI Adversary: Generates stress and failure injection scenarios to stress-test architectures."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple
import yaml

from app.ai.client import get_vertex_client, get_model_name
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


def clean_yaml(text: str) -> str:
    pattern = r"```(?:ya?ml)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


async def generate_adversary_scenario(
    arch: Architecture
) -> Tuple[Scenario, str, str, List[str]]:
    """
    Uses Vertex AI (Gemini 3.5 Flash) to generate a targeted failure scenario for the given architecture.
    Returns (Scenario object, scenario YAML string, hypothesis description, target nodes).
    """
    client = get_vertex_client()
    model = get_model_name()

    arch_dict = arch.model_dump(by_alias=True)
    arch_yaml = yaml.dump(arch_dict, sort_keys=False)
    node_ids = [n.id for n in arch.nodes]

    prompt = (
        ADVERSARY_PROMPT_TEMPLATE
        .replace("__ARCHITECTURE_YAML__", arch_yaml)
        .replace("__NODE_IDS__", ", ".join(node_ids))
    )

    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt
    )

    yaml_text = clean_yaml(response.text)
    raw_data = yaml.safe_load(yaml_text)

    if not isinstance(raw_data, dict):
        raise ValueError("Adversary output failed to parse into valid YAML dictionary.")

    # Ensure required defaults
    if "duration_s" not in raw_data:
        raw_data["duration_s"] = 300.0
    if "warmup_s" not in raw_data:
        raw_data["warmup_s"] = 30.0

    # Filter out any events referencing non-existent nodes to guarantee safety
    valid_node_set = set(node_ids)
    filtered_events = []
    target_nodes = []
    for ev in raw_data.get("events", []):
        if ev.get("target") in valid_node_set:
            filtered_events.append(ev)
            target_nodes.append(ev.get("target"))
    raw_data["events"] = filtered_events

    scenario = Scenario.model_validate(raw_data)
    clean_scenario_yaml = yaml.dump(raw_data, sort_keys=False)
    hypothesis = scenario.description or f"Adversarial attack targeting {', '.join(set(target_nodes))}"

    return scenario, clean_scenario_yaml, hypothesis, list(set(target_nodes))
