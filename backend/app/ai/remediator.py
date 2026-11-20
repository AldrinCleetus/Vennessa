"""AI Remediator: Analyzes simulation failures and produces targeted architectural fixes and YAML patches."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple
import yaml

from app.ai.client import get_vertex_client, get_model_name
from app.core.model import Architecture

REMEDIATOR_PROMPT_TEMPLATE = """You are a Principal Cloud Resilience Engineer.
The following architecture was subjected to a stress simulation and failed its Service Level Objectives (SLOs).
Analyze the root cause from the simulation findings and propose a targeted architectural fix.

### CURRENT ARCHITECTURE YAML:
```yaml
__ARCHITECTURE_YAML__
```

### SIMULATION OUTCOME & BREACH METRICS:
```json
__SIMULATION_SUMMARY_JSON__
```

### YOUR OBJECTIVE:
1. Identify the root cause node and bottleneck mechanism (e.g. queue saturation, missing replica, unbuffered write, missing backoff/jitter).
2. Propose concrete cloud architectural remediation (e.g., adding a cache layer, adding read replicas, increasing concurrency, tuning retry backoff with jitter, introducing circuit breakers or queues).
3. Return the COMPLETE updated and patched Architecture YAML that resolves the issue while remaining strictly valid.

### OUTPUT FORMAT:
Return a JSON object with this exact structure:
```json
{
  "diagnosis": "Clear explanation of why the architecture failed under load.",
  "root_cause_node": "The specific node ID causing the failure.",
  "recommended_pattern": "Name of pattern (e.g. Cache-Aside with Read Replicas)",
  "rationale": "Why this architectural change stops the outage.",
  "patched_architecture_yaml": "The FULL, updated architecture YAML string adhering to the reference format."
}
```
"""


def clean_json(text: str) -> str:
    pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


async def remediate_architecture(
    arch: Architecture,
    simulation_summary: Dict[str, Any]
) -> Tuple[str, str, str, Architecture, str]:
    """
    Uses Vertex AI (Gemini 3.5 Flash) to generate a targeted fix and patched architecture.
    Returns (diagnosis, root_cause_node, recommended_pattern, patched_architecture, patched_yaml).
    """
    client = get_vertex_client()
    model = get_model_name()

    arch_dict = arch.model_dump(by_alias=True)
    arch_yaml = yaml.dump(arch_dict, sort_keys=False)

    # Filter summary to keep the prompt clean and focused on key metrics
    condensed_summary = {
        "status": simulation_summary.get("status"),
        "passed": simulation_summary.get("passed"),
        "error_rate": simulation_summary.get("error_rate"),
        "p99_ms": simulation_summary.get("p99_ms"),
        "breach_seconds": simulation_summary.get("breach_seconds"),
        "recovery_s": simulation_summary.get("recovery_s"),
        "client_errors": simulation_summary.get("client_errors"),
        "node_errors": simulation_summary.get("node_errors"),
        "cascade": simulation_summary.get("cascade"),
    }

    prompt = (
        REMEDIATOR_PROMPT_TEMPLATE
        .replace("__ARCHITECTURE_YAML__", arch_yaml)
        .replace("__SIMULATION_SUMMARY_JSON__", json.dumps(condensed_summary, indent=2))
    )

    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt
    )

    json_text = clean_json(response.text)
    data = json.loads(json_text)

    diagnosis = data.get("diagnosis", "Bottleneck identified under simulated stress load.")
    root_cause_node = data.get("root_cause_node", "unknown")
    recommended_pattern = data.get("recommended_pattern", "Decoupling and Horizontal Scaling")
    patched_yaml_raw = data.get("patched_architecture_yaml", "")

    # Clean the YAML in case it has markdown fences
    if "```" in patched_yaml_raw:
        m = re.search(r"```(?:ya?ml)?\s*([\s\S]*?)\s*```", patched_yaml_raw)
        if m:
            patched_yaml_raw = m.group(1).strip()

    # Parse and validate the patched architecture
    patched_data = yaml.safe_load(patched_yaml_raw)
    patched_arch = Architecture.model_validate(patched_data)
    clean_patched_yaml = yaml.dump(patched_data, sort_keys=False)

    return (
        diagnosis,
        root_cause_node,
        recommended_pattern,
        patched_arch,
        clean_patched_yaml
    )
