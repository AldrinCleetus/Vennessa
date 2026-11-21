"""Deterministic validation of remediation patches.

These rules reject configurations that are wrong *by construction*. They are
delta-aware: a condition is only blocking when the patch INTRODUCES or WORSENS
it relative to the base architecture. A pre-existing weakness the patch leaves
alone (or improves) is reported as a non-blocking advisory, and the simulator
remains the final arbiter of whether the fix actually works.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from .analyze import estimate_load, LoadEstimate
from .model import Architecture, Edge, Node
from .scenario import Scenario

EPS = 1e-9


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    target: str
    message: str
    blocking: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "target": self.target,
            "message": self.message,
            "blocking": self.blocking,
        }


def _paths(ops: Iterable[Dict[str, Any]]) -> List[str]:
    return [str(op.get("path", "")) for op in ops if isinstance(op, dict)]


def _touched_node(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    return parts[1] if len(parts) >= 2 and parts[0] == "nodes" else None


def _touched_edge(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    return parts[1] if len(parts) >= 2 and parts[0] == "edges" else None


def _touches(
    paths: List[str], entity: str, identity: str, fields: tuple[str, ...] | None = None
) -> bool:
    for path in paths:
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or parts[0] != entity or parts[1] != identity:
            continue
        if fields is None or any(field in parts[2:] for field in fields):
            return True
    return False


def _worst_case_ms(edge: Edge) -> float:
    attempts = int(edge.retry.attempts or 0)
    total = edge.timeout_ms * (attempts + 1)
    if edge.retry.backoff != "none" and attempts:
        total += edge.retry.base_ms * attempts * (1.5 if edge.retry.jitter else 1.0)
    return total


def _path_rps(arch: Architecture, start: str, seen: set[str] | None = None) -> float:
    """Peak amplification factor from a traffic source to a node."""
    seen = seen or set()
    if start in seen:
        return 1.0
    seen = set(seen) | {start}
    inbound = [e for e in arch.edges if e.to == start]
    return max(
        (_path_rps(arch, e.from_, seen) * (1 + int(e.retry.attempts or 0)) for e in inbound),
        default=1.0,
    )


def _upstream_edges(arch: Architecture, node_id: str, seen: set[str] | None = None) -> List[Edge]:
    seen = seen or set()
    if node_id in seen:
        return []
    found: List[Edge] = []
    for edge in arch.edges:
        if edge.to == node_id:
            found.append(edge)
            found.extend(_upstream_edges(arch, edge.from_, seen | {node_id}))
    return found


def _base_node(base: Architecture, node_id: str) -> Node | None:
    return next((n for n in base.nodes if n.id == node_id), None)


def _base_edge(base: Architecture, edge_key: str) -> Edge | None:
    return next((e for e in base.edges if f"{e.from_}->{e.to}" == edge_key), None)


# --- badness metrics: larger is always worse --------------------------------


def _clipping(node: Node | None, load: LoadEstimate) -> float:
    """>1 means the configured rate limit is below 1.25x offered load."""
    if node is None or node.rate_limit_rps is None or node.rate_limit_rps <= 0:
        return 0.0
    return load.rps.get(node.id, 0.0) * 1.25 / node.rate_limit_rps


def _queue_ratio(node: Node | None, load: LoadEstimate, arch: Architecture) -> float:
    """>2 means the queue can serve far more work than the caller waits for."""
    if node is None:
        return 0.0
    hold_s = load.hold_ms.get(node.id, 0.0) / 1000
    return max(
        (node.queue_per_replica * hold_s / (e.timeout_ms / 1000)
         for e in arch.edges if e.to == node.id and e.timeout_ms > 0),
        default=0.0,
    )


def _inversion(node: Node | None, arch: Architecture) -> float:
    """>1 means the node's downstream work exceeds its caller's budget."""
    if node is None:
        return 0.0
    inbound = [e.timeout_ms for e in arch.edges if e.to == node.id]
    outbound = [_worst_case_ms(e) for e in arch.edges if e.from_ == node.id and e.required]
    if not inbound or not outbound:
        return 0.0
    return sum(outbound) / max(inbound)


def _retry_layers(node_id: str, arch: Architecture) -> int:
    return len({
        e.from_ for e in _upstream_edges(arch, node_id)
        if int(e.retry.attempts or 0) > 0
    })


def _cosmetic(node: Node | None, edge: Edge | None) -> float:
    """>0 means retries are configured with a cosmetic (<200ms) backoff."""
    if edge is None or int(edge.retry.attempts or 0) == 0:
        return 0.0
    if edge.retry.backoff == "none" or edge.retry.base_ms >= 200:
        return 0.0
    return 1.0 + (200.0 - edge.retry.base_ms) / 200.0


def _mid_tier_ratio(node: Node | None, load: LoadEstimate, arch: Architecture) -> float:
    if node is None or node.type not in {"service", "load_balancer"}:
        return 0.0
    downstream_s = max((e.timeout_ms for e in arch.out_edges(node.id)), default=0.0) / 1000
    if downstream_s <= 0:
        return 0.0
    return load.rps.get(node.id, 0.0) * downstream_s / max(1, node.capacity)


def validate_fix(
    base: Architecture,
    patched: Architecture,
    ops: List[Dict[str, Any]],
    scenario: Scenario | None = None,
) -> List[ValidationIssue]:
    """Apply every deterministic validation rule to a patched architecture.

    Each issue is `blocking=True` only when the patch made that metric worse
    than the base; otherwise it is an advisory (still machine-readable, but it
    does not stop the fix from being simulated and verified).
    """
    issues: List[ValidationIssue] = []
    paths = _paths(ops)
    base_load = estimate_load(base)
    load = estimate_load(patched)

    def emit(code: str, target: str, message: str, after: float, before: float,
             threshold: float) -> None:
        if after > threshold:
            issues.append(ValidationIssue(
                code, target, message, blocking=after > before + EPS
            ))

    # 0. Schema paths always block — they cannot be simulated at all.
    known_nodes = {n.id for n in base.nodes}
    known_edges = {f"{e.from_}->{e.to}" for e in base.edges}
    for path in paths:
        node, edge = _touched_node(path), _touched_edge(path)
        if node and node not in known_nodes:
            issues.append(ValidationIssue("schema_path", path, f"Unknown node id {node!r}."))
        if edge and edge not in known_edges:
            issues.append(ValidationIssue("schema_path", path, f"Unknown edge {edge!r}."))

    # 1. Rate limits must clear offered load (1s token bucket + Poisson bursts).
    for node in patched.nodes:
        if not _touches(paths, "nodes", node.id, ("rate_limit_rps",)):
            continue
        after = _clipping(node, load)
        before = _clipping(_base_node(base, node.id), base_load)
        offered = load.rps.get(node.id, 0.0)
        emit(
            "rate_limit_below_load", node.id,
            f"rate_limit_rps={node.rate_limit_rps:g} is below 1.25x offered load "
            f"({offered:.1f} rps); it will clip healthy traffic.",
            after, before, 1.0,
        )

    # 2. A bounded queue longer than the caller's timeout serves dead work.
    for node in patched.nodes:
        if not _touches(paths, "nodes", node.id, ("queue_per_replica", "replicas")):
            continue
        after = _queue_ratio(node, load, patched)
        before = _queue_ratio(_base_node(base, node.id), base_load, base)
        hold_s = load.hold_ms.get(node.id, 0.0) / 1000
        emit(
            "queue_exceeds_timeout", node.id,
            f"queue_per_replica={node.queue_per_replica} with {hold_s:.2f}s service "
            "exceeds the caller timeout; callers expire while the node still serves "
            "queued work.",
            after, before, 2.0,
        )

    # 3. Timeout inversion: child timeout x attempts must fit the parent budget.
    for node in patched.nodes:
        adjacent = [e for e in patched.edges if e.to == node.id or e.from_ == node.id]
        if not any(
            _touches(paths, "edges", f"{e.from_}->{e.to}", ("timeout_ms", "retry"))
            for e in adjacent
        ):
            continue
        after = _inversion(node, patched)
        before = _inversion(_base_node(base, node.id), base)
        emit(
            "timeout_inversion", node.id,
            f"{node.id} may spend more time downstream than callers allow; shorten "
            "child timeouts or lengthen the parent budget.",
            after, before, 1.0,
        )

    # 4. One retry layer per call path, with bounded amplification.
    for node in patched.nodes:
        path_edges = _upstream_edges(patched, node.id)
        if not any(
            _touches(paths, "edges", f"{e.from_}->{e.to}", ("retry",)) for e in path_edges
        ):
            continue
        after_layers = _retry_layers(node.id, patched)
        before_layers = _retry_layers(node.id, base)
        if after_layers > 1:
            issues.append(ValidationIssue(
                "multi_layer_retry", node.id,
                f"Retries are enabled at {after_layers} layers on the path into "
                f"{node.id}; retry at one layer only.",
                blocking=after_layers > before_layers,
            ))
        after_amp = _path_rps(patched, node.id)
        before_amp = _path_rps(base, node.id)
        bad_backoff = any(
            e.retry.backoff == "none" or not e.retry.jitter
            for e in path_edges if int(e.retry.attempts or 0) > 0
        )
        before_bad = any(
            e.retry.backoff == "none" or not e.retry.jitter
            for e in _upstream_edges(base, node.id) if int(e.retry.attempts or 0) > 0
        )
        if after_amp > 3:
            issues.append(ValidationIssue(
                "unbounded_amplification", node.id,
                f"Retries amplify {after_amp:.0f}x into {node.id} without both "
                "backoff and jitter.",
                blocking=after_amp > before_amp + EPS
                or (bad_backoff and not before_bad),
            ))

    # 5. Explicit retry bounds in any touched edge.
    for edge in patched.edges:
        edge_key = f"{edge.from_}->{edge.to}"
        if not _touches(paths, "edges", edge_key, ("retry",)):
            continue
        base_edge = _base_edge(base, edge_key)
        after_attempts = int(edge.retry.attempts or 0)
        before_attempts = int(base_edge.retry.attempts or 0) if base_edge else 0
        if after_attempts > 2:
            issues.append(ValidationIssue(
                "retry_attempts_too_high", edge_key,
                f"retry.attempts={after_attempts} exceeds the cap of 2.",
                blocking=after_attempts > before_attempts,
            ))
        after_cosmetic = _cosmetic(None, edge)
        before_cosmetic = _cosmetic(None, base_edge)
        if after_cosmetic > 0:
            issues.append(ValidationIssue(
                "backoff_cosmetic", edge_key,
                f"backoff base_ms={edge.retry.base_ms:g} is cosmetic; use at least 200ms.",
                blocking=after_cosmetic > before_cosmetic + EPS
                or (before_attempts == 0 and after_attempts > 0),
            ))

    # 6. Little's law for middle tiers.
    for node in patched.nodes:
        if node.type not in {"service", "load_balancer"} or not patched.out_edges(node.id):
            continue
        if not _touches(paths, "nodes", node.id, ("concurrency_per_replica", "replicas")):
            continue
        after = _mid_tier_ratio(node, load, patched)
        before = _mid_tier_ratio(_base_node(base, node.id), base_load, base)
        downstream_p99 = max(
            (e.timeout_ms for e in patched.out_edges(node.id)), default=0.0
        ) / 1000
        needed = load.rps.get(node.id, 0.0) * downstream_p99
        emit(
            "middle_tier_undersized", node.id,
            f"concurrency {node.capacity} < rps x downstream_p99 ({needed:.0f}); "
            "requests will queue until they time out.",
            after, before, 1.0,
        )

    return issues


def blocking_issues(issues: List[ValidationIssue]) -> List[ValidationIssue]:
    return [i for i in issues if i.blocking]


def advisory_issues(issues: List[ValidationIssue]) -> List[ValidationIssue]:
    return [i for i in issues if not i.blocking]


def issue_messages(issues: List[ValidationIssue]) -> List[str]:
    return [f"{i.code}: {i.message}" for i in issues]
