"""Safe reconciliation of an AI-generated architecture with an existing one.

Gemini is asked to return the complete updated architecture in modify mode, but
it can still omit unrelated nodes/edges/fields. This helper starts from the
current architecture and upserts generated values by identity, so anything not
explicitly changed is preserved.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .model import Architecture, _deep_merge

_REMOVE_VERBS = r"remove|delete|drop|disable|get rid of|take out|turn off"
_NEGATIONS = r"\b(?:do not|don't|dont|never|keep|preserve|retain|without)\b"


def _sentences(text: str) -> list[str]:
    return [s.strip().lower() for s in re.split(r"[.!?\n]+", text or "") if s.strip()]


def _word_present(word: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text))


def _explicit_removals(
    instruction: str, data: dict[str, Any]
) -> tuple[set[str], set[str], set[str]]:
    """Conservatively detect entities the user clearly asked to remove."""
    nodes: set[str] = set()
    edges: set[str] = set()
    sources: set[str] = set()

    for sentence in _sentences(instruction):
        if not re.search(rf"\b(?:{_REMOVE_VERBS})\b", sentence):
            continue
        if re.search(_NEGATIONS, sentence):
            continue

        tuning_only = re.search(
            r"\b(?:retry|retries|timeout|pool|jitter|backoff|weight|health check|rate limit)\b",
            sentence,
        )
        edge_like = bool(re.search(r"\b(?:edge|link|connection|route)\b|->", sentence)) and not tuning_only
        for source in (data.get("traffic") or {}).get("sources", []):
            name = str(source.get("name", ""))
            if name and _word_present(name, sentence):
                sources.add(name)

        if edge_like:
            for edge in data.get("edges", []):
                a, b = str(edge.get("from", "")), str(edge.get("to", ""))
                if a and b and _word_present(a, sentence) and _word_present(b, sentence):
                    edges.add(f"{a}->{b}")
            continue

        for node in data.get("nodes", []):
            node_id = str(node.get("id", ""))
            if not node_id:
                continue
            noun = r"node|service|cache|datastore|database|db|lb|load balancer"
            patterns = [
                rf"\b(?:{noun})\s+['\"`]?{re.escape(node_id)}\b",
                rf"\b{re.escape(node_id)}\s+(?:{noun})\b",
                rf"['\"`]{re.escape(node_id)}['\"`]",
            ]
            if any(re.search(p, sentence) for p in patterns):
                nodes.add(node_id)
    return nodes, edges, sources


def _upsert(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    identity_field: str | tuple[str, str],
    warnings: list[str],
    label: str,
) -> list[dict[str, Any]]:
    result = [copy.deepcopy(item) for item in existing]

    def key_of(item: dict[str, Any]) -> str:
        if isinstance(identity_field, tuple):
            return f"{item.get(identity_field[0])}->{item.get(identity_field[1])}"
        return str(item.get(identity_field))

    by_key = {key_of(item): item for item in result}
    for item in incoming:
        key = key_of(item)
        if key in by_key:
            _deep_merge(by_key[key], copy.deepcopy(item))
        else:
            added = copy.deepcopy(item)
            result.append(added)
            by_key[key] = added
            warnings.append(f"Added new {label} '{key}' from generated architecture.")
    return result


def reconcile_architecture(
    current: Architecture | dict[str, Any],
    generated: Architecture | dict[str, Any],
    instruction: str = "",
) -> tuple[Architecture, list[str]]:
    """Upsert generated content into a deep copy of the current architecture.

    Returns (validated architecture, warnings). Existing entities omitted by the
    model are preserved unless the instruction explicitly asks to remove them.
    """
    current_arch = current if isinstance(current, Architecture) else Architecture.model_validate(current)
    base = current_arch.model_dump(by_alias=True)
    gen = (
        generated.model_dump(by_alias=True)
        if isinstance(generated, Architecture)
        else copy.deepcopy(generated)
    )
    result = copy.deepcopy(base)
    warnings: list[str] = []

    # Scalars and nested settings not represented as identity-keyed collections.
    for key in ("name", "description"):
        if key in gen and gen[key] not in (None, ""):
            result[key] = copy.deepcopy(gen[key])
    for key in ("slo",):
        if isinstance(gen.get(key), dict):
            target = result.setdefault(key, {})
            _deep_merge(target, copy.deepcopy(gen[key]))
    for key, value in gen.items():
        if key in {"nodes", "edges", "traffic", "name", "description", "slo"}:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            _deep_merge(result[key], copy.deepcopy(value))
        else:
            result[key] = copy.deepcopy(value)

    generated_ids = {str(n.get("id")) for n in gen.get("nodes", [])}
    for node in base.get("nodes", []):
        if str(node.get("id")) not in generated_ids:
            warnings.append(f"Preserved node '{node.get('id')}' omitted by Gemini.")
    generated_edges = {f"{e.get('from')}->{e.get('to')}" for e in gen.get("edges", [])}
    for edge in base.get("edges", []):
        key = f"{edge.get('from')}->{edge.get('to')}"
        if key not in generated_edges:
            warnings.append(f"Preserved edge '{key}' omitted by Gemini.")
    generated_sources = {str(s.get("name")) for s in (gen.get("traffic") or {}).get("sources", [])}
    for source in (base.get("traffic") or {}).get("sources", []):
        if str(source.get("name")) not in generated_sources:
            warnings.append(f"Preserved traffic source '{source.get('name')}' omitted by Gemini.")

    result["nodes"] = _upsert(result.get("nodes", []), gen.get("nodes", []), "id", warnings, "node")
    result["edges"] = _upsert(result.get("edges", []), gen.get("edges", []), ("from", "to"), warnings, "edge")
    incoming_sources = (gen.get("traffic") or {}).get("sources", [])
    result.setdefault("traffic", {})["sources"] = _upsert(
        (result.get("traffic") or {}).get("sources", []),
        incoming_sources,
        "name",
        warnings,
        "traffic source",
    )

    remove_nodes, remove_edges, remove_sources = _explicit_removals(instruction, base)
    if remove_nodes:
        result["nodes"] = [n for n in result.get("nodes", []) if str(n.get("id")) not in remove_nodes]
        result["edges"] = [
            e for e in result.get("edges", [])
            if str(e.get("from")) not in remove_nodes and str(e.get("to")) not in remove_nodes
        ]
        result["traffic"]["sources"] = [
            s for s in result["traffic"].get("sources", []) if str(s.get("target")) not in remove_nodes
        ]
        for node in result["nodes"]:
            st = node.get("startup") or {}
            if st.get("requires"):
                st["requires"] = [r for r in st["requires"] if r not in remove_nodes]
            if node.get("read_replicas"):
                node["read_replicas"] = [r for r in node["read_replicas"] if r not in remove_nodes]
        warnings += [f"Removed node '{node_id}' per instruction." for node_id in sorted(remove_nodes)]
    if remove_edges:
        result["edges"] = [
            e for e in result.get("edges", [])
            if f"{e.get('from')}->{e.get('to')}" not in remove_edges
        ]
        warnings += [f"Removed edge '{key}' per instruction." for key in sorted(remove_edges)]
    if remove_sources:
        result["traffic"]["sources"] = [
            s for s in result["traffic"].get("sources", []) if str(s.get("name")) not in remove_sources
        ]
        for edge in result.get("edges", []):
            if edge.get("sources"):
                kept = [s for s in edge["sources"] if s not in remove_sources]
                edge["sources"] = kept or None
        warnings += [f"Removed traffic source '{name}' per instruction." for name in sorted(remove_sources)]

    return Architecture.model_validate(result), warnings
