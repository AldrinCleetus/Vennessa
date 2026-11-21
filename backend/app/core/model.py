"""Architecture model: schema, loading, and composition.

Everything about an architecture lives in data files (YAML or JSON).
A file can describe a full architecture, or compose others:

    include:            # pull in other architecture files
      - file: nic_current.yaml
      - file: portal_b.yaml
        prefix: b_      # namespace every id from this file
    unify:              # merge two nodes into one (shared component)
      b_auth0: auth0    # b_auth0 disappears, its edges now point at auth0
    remove:
      nodes: [b_lb]
      edges: [{from: b_api, to: b_cache}]
    override:           # deep-merge fields into existing nodes/edges
      nodes:
        auth0: {rate_limit_rps: 150}
      edges:
        - {from: lb, to: b_api, set: {weight: 1}}
    nodes: [...]        # new nodes
    edges: [...]        # new edges
    traffic:
      sources: [...]    # new traffic sources

Order of operations: includes -> own nodes/edges/traffic -> unify -> remove -> override -> validate.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

NodeType = Literal["load_balancer", "shard_router", "service", "cache", "datastore", "external"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Latency(Strict):
    p50: float = 10.0
    p99: float = 50.0

    @model_validator(mode="after")
    def _check(self) -> "Latency":
        if self.p50 <= 0 or self.p99 < self.p50:
            raise ValueError(f"latency needs 0 < p50 <= p99 (got p50={self.p50}, p99={self.p99})")
        return self

    @property
    def mu(self) -> float:
        return math.log(self.p50)

    @property
    def sigma(self) -> float:
        # p99 of a lognormal is exp(mu + 2.326 * sigma)
        return (math.log(self.p99) - math.log(self.p50)) / 2.326

    @property
    def mean(self) -> float:
        return math.exp(self.mu + self.sigma**2 / 2)


class Startup(Strict):
    time_s: float = 10.0
    requires: list[str] = Field(default_factory=list)


class HealthCheck(Strict):
    interval_s: float = 10.0
    # a check fails when the node's error rate over the interval exceeds this
    fail_threshold: float = 0.5
    # after this many failed checks in a row, one replica is pulled and restarted
    unhealthy_after: int = 3
    # shallow: only the node's own rejections count. deep: downstream failures count too
    # (a deep check makes the LB pull healthy replicas when a dependency is down)
    deep: bool = False


class Node(Strict):
    id: str
    type: NodeType = "service"
    description: str = ""
    replicas: int = 1
    # concurrent requests one replica can work on
    concurrency_per_replica: int = 10
    # requests one replica can hold waiting for a slot; beyond this they are rejected
    queue_per_replica: int = 100
    # own processing time (excluding downstream calls)
    latency_ms: Latency = Field(default_factory=Latency)
    # if true, a request holds its slot while waiting on downstream calls (thread pool style)
    holds_slot_during_calls: bool = True
    startup: Startup = Field(default_factory=Startup)
    health_check: HealthCheck | None = None
    # external services: requests per second before 429s
    rate_limit_rps: float | None = None
    # caches
    warm_hit_rate: float = 0.0
    cold_hit_rate: float = 0.0
    warmup_s: float = 300.0
    # datastores: read replicas of this node (the primary). Reads on edges with a
    # write_ratio go to a healthy replica; writes always go to the primary.
    read_replicas: list[str] = Field(default_factory=list)
    # when every replica is down, send reads to the primary (true) or fail them (false)
    read_fallback: bool = True
    tags: list[str] = Field(default_factory=list)

    @property
    def capacity(self) -> int:
        return self.replicas * self.concurrency_per_replica


class CircuitBreaker(Strict):
    """Optional additive control that fast-fails calls to a struggling dependency."""

    failure_threshold: float = 0.5
    min_calls: int = 10
    open_s: float = 10.0


class Retry(Strict):
    attempts: int = 0
    backoff: Literal["none", "fixed", "exponential"] = "none"
    base_ms: float = 100.0
    max_ms: float = 10_000.0
    jitter: bool = False
    # additive: cap retries to this share of calls in a rolling second
    budget_ratio: float | None = Field(default=None, ge=0.0, le=1.0)


class Edge(Strict):
    from_: str = Field(alias="from")
    to: str
    # service: probability the call is made per request.
    p: float = 1.0
    # load_balancer: routing weight. shard_router: share of the key space on this shard.
    weight: float = 1.0
    timeout_ms: float = 5_000.0
    retry: Retry = Field(default_factory=Retry)
    # look in this cache first; only call `to` on a miss (or if the cache is unavailable)
    via_cache: str | None = None
    cache_timeout_ms: float = 200.0
    # if false, a failure of this call does not fail the request
    required: bool = True
    # max concurrent in-flight calls on this edge (e.g. a DB connection pool); None = unlimited
    pool: int | None = None
    # additive controls; None/false keeps legacy behaviour
    circuit_breaker: CircuitBreaker | None = None
    deadline_propagation: bool = False
    pool_timeout_ms: float | None = None
    # only requests from these traffic sources use this edge (host/path routing); None = all
    sources: list[str] | None = None
    # share of calls that are writes (0..1). Only matters when `to` has read_replicas:
    # writes go to `to`, the rest are reads served by a replica. None = all calls hit `to`.
    write_ratio: float | None = Field(default=None, ge=0.0, le=1.0)

    @property
    def key(self) -> tuple[str, str]:
        return (self.from_, self.to)


class Source(Strict):
    name: str
    target: str
    rps: float
    # the client gives up after this long
    timeout_ms: float = 30_000.0


class Traffic(Strict):
    sources: list[Source] = Field(default_factory=list)


class SLO(Strict):
    error_rate_max: float = 0.01
    p99_ms_max: float = 3_000.0
    # after the last injected event, the system must be back within SLO in this many seconds
    recover_within_s: float = 300.0
    # error rate and p99 are evaluated over a rolling window of this many seconds
    window_s: int = 60


class Architecture(Strict):
    name: str
    description: str = ""
    nodes: list[Node]
    edges: list[Edge] = Field(default_factory=list)
    traffic: Traffic = Field(default_factory=Traffic)
    slo: SLO = Field(default_factory=SLO)

    @model_validator(mode="after")
    def _validate_refs(self) -> "Architecture":
        ids = [n.id for n in self.nodes]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate node ids: {sorted(dupes)}")
        known = set(ids)
        problems: list[str] = []
        for e in self.edges:
            for ref in (e.from_, e.to, e.via_cache):
                if ref is not None and ref not in known:
                    problems.append(f"edge {e.from_}->{e.to} references unknown node '{ref}'")
            if e.via_cache and self.node(e.via_cache).type != "cache":
                problems.append(f"edge {e.from_}->{e.to}: via_cache '{e.via_cache}' is not a cache")
        for n in self.nodes:
            for r in n.startup.requires:
                if r not in known:
                    problems.append(f"node {n.id} startup requires unknown node '{r}'")
            for r in n.read_replicas:
                if r not in known:
                    problems.append(f"node {n.id} read_replicas references unknown node '{r}'")
                elif r == n.id:
                    problems.append(f"node {n.id} lists itself as a read replica")
                elif self.node(r).type != "datastore" or n.type != "datastore":
                    problems.append(f"node {n.id} read replica '{r}': primary and replicas must be datastores")
        for n in self.nodes:
            if n.type == "shard_router" and not any(e.from_ == n.id for e in self.edges):
                problems.append(f"shard_router {n.id} has no shards (no outgoing edges)")
        for s in self.traffic.sources:
            if s.target not in known:
                problems.append(f"traffic source {s.name} targets unknown node '{s.target}'")
        source_names = {s.name for s in self.traffic.sources}
        for e in self.edges:
            for name in e.sources or []:
                if name not in source_names:
                    problems.append(f"edge {e.from_}->{e.to} routes unknown traffic source '{name}'")
        keys = [e.key for e in self.edges]
        dup_edges = {k for k in keys if keys.count(k) > 1}
        if dup_edges:
            problems.append(f"duplicate edges: {sorted(dup_edges)}")
        if problems:
            raise ValueError("invalid architecture:\n  " + "\n  ".join(problems))
        return self

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def has_node(self, node_id: str) -> bool:
        return any(n.id == node_id for n in self.nodes)

    def out_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.from_ == node_id]

    def in_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.to == node_id or e.via_cache == node_id]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_defaults=False)


# --------------------------------------------------------------------------- loading


def read_data(path: Path) -> dict[str, Any]:
    """YAML is a superset of JSON, so this reads both."""
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def load_architecture(path: str | Path) -> Architecture:
    path = Path(path).resolve()
    raw = _resolve(path, stack=())
    return Architecture.model_validate(raw)


def _resolve(path: Path, stack: tuple[Path, ...]) -> dict[str, Any]:
    if path in stack:
        chain = " -> ".join(p.name for p in (*stack, path))
        raise ValueError(f"include cycle: {chain}")
    data = read_data(path)

    out: dict[str, Any] = {"nodes": [], "edges": [], "traffic": {"sources": []}}
    for inc in data.get("include", []) or []:
        if isinstance(inc, str):
            inc = {"file": inc}
        child = _resolve((path.parent / inc["file"]).resolve(), (*stack, path))
        prefix = inc.get("prefix", "")
        if prefix:
            child = _prefix(child, prefix)
        out["nodes"] += child["nodes"]
        out["edges"] += child["edges"]
        out["traffic"]["sources"] += child["traffic"]["sources"]
        # first included file provides defaults for name/slo unless overridden below
        out.setdefault("slo", child.get("slo"))
        out.setdefault("description", child.get("description", ""))

    out["nodes"] += copy.deepcopy(data.get("nodes", []) or [])
    out["edges"] += copy.deepcopy(data.get("edges", []) or [])
    out["traffic"]["sources"] += copy.deepcopy((data.get("traffic") or {}).get("sources", []) or [])

    for alias, target in (data.get("unify") or {}).items():
        _unify(out, alias, target, path)

    removals = data.get("remove") or {}
    _remove(out, removals, path)

    overrides = data.get("override") or {}
    _override(out, overrides, path)

    out["name"] = data.get("name") or path.stem
    if data.get("description"):
        out["description"] = data["description"]
    if data.get("slo"):
        out["slo"] = data["slo"]
    if out.get("slo") is None:
        out.pop("slo", None)
    return out


def _prefix(arch: dict[str, Any], prefix: str) -> dict[str, Any]:
    arch = copy.deepcopy(arch)
    p = lambda x: f"{prefix}{x}"  # noqa: E731
    for n in arch["nodes"]:
        n["id"] = p(n["id"])
        st = n.get("startup")
        if st and st.get("requires"):
            st["requires"] = [p(r) for r in st["requires"]]
        if n.get("read_replicas"):
            n["read_replicas"] = [p(r) for r in n["read_replicas"]]
    for e in arch["edges"]:
        e["from"] = p(e["from"])
        e["to"] = p(e["to"])
        if e.get("via_cache"):
            e["via_cache"] = p(e["via_cache"])
        if e.get("sources"):
            e["sources"] = [p(s) for s in e["sources"]]
    for s in arch["traffic"]["sources"]:
        s["name"] = p(s["name"])
        s["target"] = p(s["target"])
    return arch


def _unify(arch: dict[str, Any], alias: str, target: str, path: Path) -> None:
    ids = {n["id"] for n in arch["nodes"]}
    if alias not in ids:
        raise ValueError(f"{path.name}: unify: unknown node '{alias}'")
    if target not in ids:
        raise ValueError(f"{path.name}: unify: unknown target node '{target}'")
    arch["nodes"] = [n for n in arch["nodes"] if n["id"] != alias]
    sub = lambda x: target if x == alias else x  # noqa: E731
    for n in arch["nodes"]:
        st = n.get("startup")
        if st and st.get("requires"):
            st["requires"] = list(dict.fromkeys(sub(r) for r in st["requires"]))
        if n.get("read_replicas"):
            n["read_replicas"] = [r for r in dict.fromkeys(sub(r) for r in n["read_replicas"]) if r != n["id"]]
    seen: set[tuple[str, str]] = set()
    edges = []
    for e in arch["edges"]:
        e["from"], e["to"] = sub(e["from"]), sub(e["to"])
        if e.get("via_cache"):
            e["via_cache"] = sub(e["via_cache"])
        k = (e["from"], e["to"])
        if e["from"] == e["to"] or k in seen:
            continue  # merged duplicates: keep the first definition
        seen.add(k)
        edges.append(e)
    arch["edges"] = edges
    for s in arch["traffic"]["sources"]:
        s["target"] = sub(s["target"])


def _remove(arch: dict[str, Any], removals: dict[str, Any], path: Path) -> None:
    gone = set(removals.get("nodes", []) or [])
    unknown = gone - {n["id"] for n in arch["nodes"]}
    if unknown:
        raise ValueError(f"{path.name}: remove: unknown nodes {sorted(unknown)}")
    arch["nodes"] = [n for n in arch["nodes"] if n["id"] not in gone]
    arch["edges"] = [e for e in arch["edges"] if e["from"] not in gone and e["to"] not in gone]
    arch["traffic"]["sources"] = [s for s in arch["traffic"]["sources"] if s["target"] not in gone]
    for n in arch["nodes"]:
        st = n.get("startup")
        if st and st.get("requires"):
            st["requires"] = [r for r in st["requires"] if r not in gone]
        if n.get("read_replicas"):
            n["read_replicas"] = [r for r in n["read_replicas"] if r not in gone]
    for spec in removals.get("edges", []) or []:
        k = (spec["from"], spec["to"])
        before = len(arch["edges"])
        arch["edges"] = [e for e in arch["edges"] if (e["from"], e["to"]) != k]
        if len(arch["edges"]) == before:
            raise ValueError(f"{path.name}: remove: unknown edge {k[0]}->{k[1]}")
    for name in removals.get("sources", []) or []:
        arch["traffic"]["sources"] = [s for s in arch["traffic"]["sources"] if s["name"] != name]


def _override(arch: dict[str, Any], overrides: dict[str, Any], path: Path) -> None:
    by_id = {n["id"]: n for n in arch["nodes"]}
    for node_id, patch in (overrides.get("nodes") or {}).items():
        if node_id not in by_id:
            raise ValueError(f"{path.name}: override: unknown node '{node_id}'")
        _deep_merge(by_id[node_id], patch)
    for spec in overrides.get("edges", []) or []:
        k = (spec["from"], spec["to"])
        match = [e for e in arch["edges"] if (e["from"], e["to"]) == k]
        if not match:
            raise ValueError(f"{path.name}: override: unknown edge {k[0]}->{k[1]}")
        _deep_merge(match[0], spec.get("set", {}))
    for spec in overrides.get("sources", []) or []:
        match = [s for s in arch["traffic"]["sources"] if s["name"] == spec["name"]]
        if not match:
            raise ValueError(f"{path.name}: override: unknown traffic source '{spec['name']}'")
        _deep_merge(match[0], spec.get("set", {}))


def _deep_merge(dst: dict[str, Any], patch: dict[str, Any]) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
