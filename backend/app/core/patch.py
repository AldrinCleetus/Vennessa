"""Atomic, validated patches for Architecture models.

Patch paths use stable identities rather than list indexes:

    /nodes/<id>/...
    /edges/<from>-><to>/...
    /traffic/sources/<name>/...
    /slo/<field>
    /name, /description

Supported operations:

    set      deep-merge dict values, replace scalars/lists
    replace  replace the value outright
    add      append to a list (or create an entity at an identity path)
    remove   delete a value/entity, cleaning up references
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from pydantic import ValidationError

from .model import Architecture, _deep_merge

OPS = {"set", "replace", "add", "remove"}


class PatchError(ValueError):
    """Base class for patch failures."""


class PatchConflictError(PatchError):
    """The patch was made against a different architecture version."""


class PatchValidationError(PatchError):
    """The patch is malformed or the patched architecture is invalid."""


def _as_arch(arch: Architecture | dict[str, Any]) -> Architecture:
    if isinstance(arch, Architecture):
        return arch
    return Architecture.model_validate(arch)


def architecture_fingerprint(arch: Architecture | dict[str, Any]) -> str:
    """SHA-256 of the canonical (key-sorted) architecture representation."""
    model = _as_arch(arch)
    canonical = json.dumps(
        model.model_dump(by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- diff


def _entity_items(data: dict[str, Any], kind: str) -> list[tuple[str, dict[str, Any]]]:
    if kind == "nodes":
        items = data.get("nodes") or []
        return [(str(item["id"]), item) for item in items]
    if kind == "edges":
        items = data.get("edges") or []
        return [(f"{item['from']}->{item['to']}", item) for item in items]
    if kind == "sources":
        items = (data.get("traffic") or {}).get("sources") or []
        return [(str(item["name"]), item) for item in items]
    return []


def _prefix_for(kind: str) -> str:
    return {"nodes": "/nodes", "edges": "/edges", "sources": "/traffic/sources"}[kind]


def _flatten_fields(prefix: str, value: dict[str, Any]) -> dict[str, Any]:
    """Flatten a mapping into stable leaf paths (nested dicts are expanded)."""
    out: dict[str, Any] = {}
    for key, val in value.items():
        path = f"{prefix}/{key}"
        if isinstance(val, dict):
            out.update(_flatten_fields(path, val))
        else:
            out[path] = val
    return out


def _flatten(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("name", "description"):
        if key in data:
            out[f"/{key}"] = data[key]
    for key, val in (data.get("slo") or {}).items():
        out[f"/slo/{key}"] = val
    for kind in ("nodes", "edges", "sources"):
        for identity, entity in _entity_items(data, kind):
            out.update(_flatten_fields(f"{_prefix_for(kind)}/{identity}", entity))
    return out


def diff_architectures(
    before: Architecture | dict[str, Any],
    after: Architecture | dict[str, Any],
) -> dict[str, Any]:
    """Flat, identity-based diff between two architectures."""
    a = _as_arch(before).model_dump(by_alias=True)
    b = _as_arch(after).model_dump(by_alias=True)

    changes: list[dict[str, Any]] = []
    for kind in ("nodes", "edges", "sources"):
        a_entities = dict(_entity_items(a, kind))
        b_entities = dict(_entity_items(b, kind))
        prefix = _prefix_for(kind)
        for identity in sorted(set(a_entities) | set(b_entities)):
            if identity not in a_entities:
                changes.append({
                    "op": "add",
                    "path": f"{prefix}/{identity}",
                    "before": None,
                    "after": copy.deepcopy(b_entities[identity]),
                })
            elif identity not in b_entities:
                changes.append({
                    "op": "remove",
                    "path": f"{prefix}/{identity}",
                    "before": copy.deepcopy(a_entities[identity]),
                    "after": None,
                })

    flat_a, flat_b = _flatten(a), _flatten(b)
    for path in sorted(set(flat_a) | set(flat_b)):
        # Whole-entity add/remove is already reported above.
        if path.startswith(("/nodes/", "/edges/", "/traffic/sources/")):
            parts = path.split("/")
            is_entity_root = (
                len(parts) == 3 and path.startswith(("/nodes/", "/edges/"))
            ) or (len(parts) == 4 and path.startswith("/traffic/sources/"))
            if is_entity_root:
                continue
        old, new = flat_a.get(path), flat_b.get(path)
        if old != new:
            changes.append({"op": "set", "path": path, "before": old, "after": new})
    return {
        "changes": changes,
        "fingerprint_before": architecture_fingerprint(a),
        "fingerprint_after": architecture_fingerprint(b),
    }


# --------------------------------------------------------------------------- paths


def _path_parts(path: Any) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise PatchValidationError(f"Invalid patch path: {path!r} (must start with '/')")
    return [part for part in path.split("/") if part]


def _find(collection: list[dict[str, Any]], kind: str, identity: str) -> tuple[int, dict[str, Any]] | None:
    for index, item in enumerate(collection):
        if kind == "nodes" and str(item.get("id")) == identity:
            return index, item
        if kind == "edges" and f"{item.get('from')}->{item.get('to')}" == identity:
            return index, item
        if kind == "sources" and str(item.get("name")) == identity:
            return index, item
    return None


def _identity_value(kind: str, identity: str) -> dict[str, Any]:
    if kind == "nodes":
        return {"id": identity}
    if kind == "edges":
        if "->" not in identity:
            raise PatchValidationError(f"Invalid edge identity {identity!r}; expected '<from>-><to>'")
        source, target = identity.split("->", 1)
        if not source or not target:
            raise PatchValidationError(f"Invalid edge identity {identity!r}; expected '<from>-><to>'")
        return {"from": source, "to": target}
    return {"name": identity}


def _collection(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    if kind == "sources":
        return data.setdefault("traffic", {}).setdefault("sources", [])
    return data.setdefault(kind, [])


def _descend(obj: dict[str, Any], parts: list[str], path: str, create: bool) -> tuple[dict[str, Any], str]:
    """Walk all but the last path segment, returning the containing dict and key."""
    current = obj
    for part in parts[:-1]:
        nxt = current.get(part)
        if nxt is None:
            if not create:
                raise PatchValidationError(f"Patch path {path!r} has missing intermediate '{part}'")
            nxt = {}
            current[part] = nxt
        if not isinstance(nxt, dict):
            raise PatchValidationError(f"Patch path {path!r} traverses non-object '{part}'")
        current = nxt
    return current, parts[-1]


def _set_value(container: dict[str, Any], key: str, value: Any, merge_dicts: bool) -> None:
    if merge_dicts and isinstance(container.get(key), dict) and isinstance(value, dict):
        _deep_merge(container[key], value)
    else:
        container[key] = copy.deepcopy(value)


def _clean_removed_node(data: dict[str, Any], node_id: str) -> None:
    """Mirror model._remove semantics for a removed node."""
    gone = {node_id}
    data["nodes"] = [n for n in data.get("nodes", []) if n.get("id") not in gone]
    data["edges"] = [
        e for e in data.get("edges", [])
        if e.get("from") not in gone and e.get("to") not in gone and e.get("via_cache") not in gone
    ]
    if "traffic" in data:
        data["traffic"]["sources"] = [
            s for s in (data["traffic"].get("sources") or []) if s.get("target") not in gone
        ]
    for node in data.get("nodes", []):
        st = node.get("startup") or {}
        if st.get("requires"):
            st["requires"] = [r for r in st["requires"] if r not in gone]
        if node.get("read_replicas"):
            node["read_replicas"] = [r for r in node["read_replicas"] if r not in gone]


def _clean_removed_source(data: dict[str, Any], name: str) -> None:
    sources = (data.get("traffic") or {}).get("sources", [])
    data["traffic"]["sources"] = [s for s in sources if s.get("name") != name]
    for edge in data.get("edges", []):
        if edge.get("sources") and name in edge["sources"]:
            edge["sources"] = [s for s in edge["sources"] if s != name]
            if not edge["sources"]:
                edge["sources"] = None


# --------------------------------------------------------------------------- apply


def _apply_op(data: dict[str, Any], op: dict[str, Any]) -> None:
    if not isinstance(op, dict):
        raise PatchValidationError(f"Patch op must be an object, got {type(op).__name__}")
    kind = op.get("op")
    if kind not in OPS:
        raise PatchValidationError(f"Unsupported patch op {kind!r}; expected one of {sorted(OPS)}")
    path = op.get("path")
    parts = _path_parts(path)
    value = op.get("value")

    # Collection root: /nodes, /edges, /traffic/sources
    if len(parts) == 1 and parts[0] in ("nodes", "edges"):
        target_kind = parts[0]
        collection = _collection(data, target_kind)
        if kind == "add":
            if not isinstance(value, dict):
                raise PatchValidationError(f"add {path!r} requires an object value")
            collection.append(copy.deepcopy(value))
            return
        if kind in ("set", "replace"):
            if not isinstance(value, list):
                raise PatchValidationError(f"{kind} {path!r} requires a list value")
            data["nodes" if target_kind == "nodes" else "edges"] = copy.deepcopy(value)
            return
        raise PatchValidationError(f"remove {path!r} requires an identity (e.g. /nodes/<id>)")
    if len(parts) == 2 and parts[0] == "traffic" and parts[1] == "sources":
        collection = _collection(data, "sources")
        if kind == "add":
            if not isinstance(value, dict):
                raise PatchValidationError(f"add {path!r} requires an object value")
            collection.append(copy.deepcopy(value))
            return
        if kind in ("set", "replace"):
            if not isinstance(value, list):
                raise PatchValidationError(f"{kind} {path!r} requires a list value")
            data["traffic"]["sources"] = copy.deepcopy(value)
            return
        raise PatchValidationError(f"remove {path!r} requires an identity (e.g. /traffic/sources/<name>)")

    # Entity root or entity field: /nodes/<id>[/field...], etc.
    entity_kind: str | None = None
    identity: str | None = None
    rest: list[str] = []
    if parts[0] in ("nodes", "edges") and len(parts) >= 2:
        entity_kind, identity, rest = parts[0], parts[1], parts[2:]
    elif parts[0] == "traffic" and len(parts) >= 3 and parts[1] == "sources":
        entity_kind, identity, rest = "sources", parts[2], parts[3:]
    if entity_kind is not None and identity is not None:
        collection = _collection(data, entity_kind)
        found = _find(collection, entity_kind, identity)
        if kind == "add" and not rest and found is None:
            if not isinstance(value, dict):
                raise PatchValidationError(f"add {path!r} requires an object value")
            new_entity = _identity_value(entity_kind, identity)
            _deep_merge(new_entity, value)
            collection.append(new_entity)
            return
        if kind == "remove" and not rest:
            if found is None:
                raise PatchValidationError(f"Cannot remove missing entity at {path!r}")
            if entity_kind == "nodes":
                _clean_removed_node(data, identity)
            elif entity_kind == "sources":
                _clean_removed_source(data, identity)
            else:
                collection.pop(found[0])
            return
        if found is None:
            raise PatchValidationError(f"Patch path {path!r} references missing entity")
        entity = found[1]
        if not rest:
            if kind == "add":
                raise PatchValidationError(f"add {path!r}: entity already exists")
            if kind == "remove":
                raise PatchValidationError(f"remove {path!r} must be an entity root or field")
            if not isinstance(value, dict):
                raise PatchValidationError(f"{kind} {path!r} requires an object value")
            if kind == "replace":
                collection[found[0]] = copy.deepcopy(value)
            else:
                _deep_merge(entity, value)
            return
        container, key = _descend(entity, rest, str(path), create=(kind != "remove"))

        if kind == "remove":
            if key not in container:
                raise PatchValidationError(f"remove {path!r}: '{key}' does not exist")
            if isinstance(container[key], list) and "value" in op:
                container[key] = [v for v in container[key] if v != op["value"]]
            else:
                del container[key]
            return
        if kind == "add":
            current = container.get(key)
            if current is None:
                container[key] = copy.deepcopy(value) if not isinstance(value, list) else list(value)
            elif isinstance(current, list):
                current.extend(copy.deepcopy(value) if isinstance(value, list) else [copy.deepcopy(value)])
            else:
                raise PatchValidationError(f"add {path!r}: target is not a list")
            return
        if kind == "replace":
            _set_value(container, key, value, merge_dicts=False)
        else:
            _set_value(container, key, value, merge_dicts=True)
        return

    # Top-level /slo/... and simple fields
    container, key = _descend(data, parts, str(path), create=(kind != "remove"))
    if kind == "remove":
        if key not in container:
            raise PatchValidationError(f"remove {str(path)!r}: '{key}' does not exist")
        if isinstance(container[key], list) and "value" in op:
            container[key] = [v for v in container[key] if v != op["value"]]
        else:
            del container[key]
    elif kind == "add":
        current = container.get(key)
        if current is None:
            container[key] = copy.deepcopy(value) if not isinstance(value, list) else list(value)
        elif isinstance(current, list):
            current.extend(copy.deepcopy(value) if isinstance(value, list) else [copy.deepcopy(value)])
        else:
            raise PatchValidationError(f"add {str(path)!r}: target is not a list")
    else:
        _set_value(container, key, value, merge_dicts=(kind == "set"))


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = " -> ".join(str(p) for p in err.get("loc", []))
        msg = err.get("msg", "")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return " | ".join(parts) if parts else str(exc)


def apply_architecture_patch(
    arch: Architecture | dict[str, Any],
    patch: dict[str, Any],
) -> tuple[Architecture, dict[str, Any], list[str]]:
    """Apply a patch atomically and return (patched_arch, diff, applied_fix_ids)."""
    original = _as_arch(arch)
    if hasattr(patch, "model_dump"):
        patch = patch.model_dump()
    if not isinstance(patch, dict):
        raise PatchValidationError(f"patch must be an object, got {type(patch).__name__}")

    expected = patch.get("base_fingerprint")
    if expected and expected != architecture_fingerprint(original):
        raise PatchConflictError(
            f"Architecture changed since the patch was generated "
            f"(expected {expected}, current {architecture_fingerprint(original)})"
        )

    ops = patch.get("ops") or []
    if not isinstance(ops, list):
        raise PatchValidationError("patch.ops must be a list")

    data = copy.deepcopy(original.model_dump(by_alias=True))
    for index, op in enumerate(ops):
        try:
            _apply_op(data, op)
        except PatchValidationError as exc:
            raise PatchValidationError(f"ops[{index}]: {exc}") from exc

    try:
        patched = Architecture.model_validate(data)
    except ValidationError as exc:
        raise PatchValidationError(_format_validation_error(exc)) from exc

    applied: list[str] = []
    for candidate in (patch.get("fix_ids") or [], patch.get("source_fix_id"), *(op.get("fix_id") for op in ops if isinstance(op, dict))):
        if candidate and candidate not in applied:
            applied.append(candidate)
    return patched, diff_architectures(original, patched), applied
