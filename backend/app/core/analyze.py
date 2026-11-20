"""Static analysis: find weak spots in an architecture without simulating it.

Each rule looks at the model data only. Findings have a stable key (rule, target)
so two architectures can be diffed (what a merge introduces / removes).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .model import Architecture, Edge

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
ROUTERS = {"load_balancer", "shard_router"}  # split traffic across out-edges by weight


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    target: str
    message: str
    fix: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule, self.target)


# --------------------------------------------------------------------------- load estimate


@dataclass
class LoadEstimate:
    rps: dict[str, float]
    hold_ms: dict[str, float]  # mean time a request occupies a slot
    util: dict[str, float]  # in-flight / capacity (Little's law)


def estimate_load(arch: Architecture, cold_caches: bool = False) -> LoadEstimate:
    """Expected steady-state request rate and utilization per node."""
    rps: dict[str, float] = {n.id: 0.0 for n in arch.nodes}

    def hit(cache_id: str) -> float:
        c = arch.node(cache_id)
        return c.cold_hit_rate if cold_caches else c.warm_hit_rate

    def push(node_id: str, r: float, src: str, depth: int = 0) -> None:
        if depth > 50 or r <= 0:
            return
        rps[node_id] += r
        n = arch.node(node_id)
        edges = [e for e in arch.out_edges(node_id) if e.sources is None or src in e.sources]
        if n.type in ROUTERS:
            total_w = sum(e.weight for e in edges) or 1.0
            for e in edges:
                push(e.to, r * e.weight / total_w, src, depth + 1)
            return
        for e in edges:
            share = r * e.p
            if e.via_cache:
                push(e.via_cache, share, src, depth + 1)
                share *= 1 - hit(e.via_cache)
            replicas = arch.node(e.to).read_replicas
            if e.write_ratio is not None and replicas:
                reads = share * (1 - e.write_ratio)
                for rep in replicas:
                    push(rep, reads / len(replicas), src, depth + 1)
                share -= reads
            push(e.to, share, src, depth + 1)

    for s in arch.traffic.sources:
        push(s.target, s.rps, s.name)

    @lru_cache(maxsize=None)
    def latency(node_id: str, depth: int = 0) -> float:
        """Mean end-to-end latency of a request to this node, in ms."""
        n = arch.node(node_id)
        own = n.latency_ms.mean
        if depth > 50:
            return own
        return own + downstream(node_id, depth)

    def downstream(node_id: str, depth: int) -> float:
        n = arch.node(node_id)
        edges = arch.out_edges(node_id)
        if n.type in ROUTERS:
            total_w = sum(e.weight for e in edges) or 1.0
            return sum(e.weight / total_w * call(e, depth) for e in edges)
        return sum(e.p * call(e, depth) for e in edges)

    def call(e: Edge, depth: int) -> float:
        t = 0.0
        miss = 1.0
        if e.via_cache:
            t += min(e.cache_timeout_ms, latency(e.via_cache, depth + 1))
            miss = 1 - hit(e.via_cache)
        return t + miss * min(e.timeout_ms, latency(e.to, depth + 1))

    hold: dict[str, float] = {}
    util: dict[str, float] = {}
    for n in arch.nodes:
        hold[n.id] = n.latency_ms.mean + (downstream(n.id, 0) if n.holds_slot_during_calls else 0.0)
        in_flight = rps[n.id] * hold[n.id] / 1000
        util[n.id] = in_flight / n.capacity if n.capacity else float("inf")
    return LoadEstimate(rps=rps, hold_ms=hold, util=util)


# --------------------------------------------------------------------------- graph helpers


def reachable(arch: Architecture) -> set[str]:
    seen: set[str] = set()
    stack = [s.target for s in arch.traffic.sources]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(arch.node(cur).read_replicas)
        for e in arch.out_edges(cur):
            stack.append(e.to)
            if e.via_cache:
                stack.append(e.via_cache)
    return seen


def critical_path_nodes(arch: Architecture) -> set[str]:
    """Nodes whose failure fails requests: reachable via required edges.
    A cache is not critical (a miss falls through); LB backends are critical
    only if they are the LB's only backend. Every shard is critical: a down
    shard fails all requests for its keys."""
    seen: set[str] = set()
    stack = [s.target for s in arch.traffic.sources]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        edges = arch.out_edges(cur)
        if arch.node(cur).type == "load_balancer":
            if len(edges) == 1:
                stack.append(edges[0].to)
            else:
                stack.extend(e.to for e in edges)  # each is still on *a* critical path
            continue
        stack.extend(e.to for e in edges if e.required)
    return seen


def startup_cycles(arch: Architecture) -> list[list[str]]:
    graph = {n.id: n.startup.requires for n in arch.nodes}
    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(v: str) -> None:
        state[v] = 1
        path.append(v)
        for w in graph.get(v, []):
            if state.get(w) == 1:
                cycles.append(path[path.index(w):] + [w])
            elif w not in state:
                visit(w)
        path.pop()
        state[v] = 2

    for v in graph:
        if v not in state:
            visit(v)
    # dedupe rotations
    uniq, keys = [], set()
    for c in cycles:
        k = frozenset(c)
        if k not in keys:
            keys.add(k)
            uniq.append(c)
    return uniq


def worst_case_ms(arch: Architecture, e: Edge) -> float:
    r = e.retry
    total = e.timeout_ms * (r.attempts + 1)
    for a in range(1, r.attempts + 1):
        if r.backoff == "fixed":
            total += r.base_ms
        elif r.backoff == "exponential":
            total += min(r.max_ms, r.base_ms * 2 ** (a - 1))
    return total


# --------------------------------------------------------------------------- rules


def analyze(arch: Architecture) -> list[Finding]:
    f: list[Finding] = []
    live = reachable(arch)
    critical = critical_path_nodes(arch)
    warm = estimate_load(arch)
    cold = estimate_load(arch, cold_caches=True)
    has_caches = any(n.type == "cache" for n in arch.nodes)

    for n in arch.nodes:
        if n.id not in live:
            f.append(Finding("unreachable", "low", n.id, f"{n.id} receives no traffic from any source.",
                             "Check edges/traffic, or remove the node."))
            continue

        # single points of failure
        if n.id in critical and n.replicas == 1 and n.type != "external":
            f.append(Finding("spof", "high", n.id,
                             f"{n.id} has 1 replica and sits on a required path.",
                             "Add replicas, or make callers degrade gracefully (required: false / fallback)."))

        # capacity headroom
        u = warm.util[n.id]
        if u > 1.0:
            f.append(Finding("over_capacity", "critical", n.id,
                             f"{n.id} needs ~{u:.0%} of its capacity at baseline traffic "
                             f"({warm.rps[n.id]:.0f} rps x {warm.hold_ms[n.id]:.0f} ms / {n.capacity} slots).",
                             "Add replicas/concurrency, or cut per-request hold time."))
        elif u > 0.7:
            f.append(Finding("low_headroom", "high", n.id,
                             f"{n.id} runs at ~{u:.0%} utilization at baseline; a ~{1 / u:.1f}x spike saturates it.",
                             "Add capacity headroom or autoscaling."))
        if has_caches and cold.util[n.id] > 1.0 and u <= 1.0:
            f.append(Finding("cold_cache_stampede", "high", n.id,
                             f"With cold caches {n.id} needs ~{cold.util[n.id]:.0%} of its capacity "
                             f"(vs ~{u:.0%} warm). A cache restart can overload it.",
                             "Warm caches before taking traffic, add request coalescing, or limit miss traffic."))

        # rate-limited externals
        if n.rate_limit_rps:
            for label, est in (("baseline", warm), ("cold-cache", cold)):
                ratio = est.rps[n.id] / n.rate_limit_rps
                if ratio > 0.7:
                    sev = "critical" if ratio > 1 else "high"
                    f.append(Finding(f"rate_limit_{label.replace('-', '_')}", sev, n.id,
                                     f"{n.id} gets ~{est.rps[n.id]:.0f} rps at {label} vs a limit of "
                                     f"{n.rate_limit_rps:.0f} rps ({ratio:.0%}).",
                                     "Cache responses (e.g. tokens/JWKS), batch calls, or raise the limit."))

        # startup dependencies on things you don't control
        ext = [r for r in n.startup.requires if arch.node(r).type == "external"]
        if ext:
            f.append(Finding("boot_needs_external", "medium", n.id,
                             f"{n.id} can't start while {', '.join(ext)} is down.",
                             "Start in a degraded mode and connect lazily."))

        # load balancer backends without health checks
        if n.type == "load_balancer":
            for e in arch.out_edges(n.id):
                if arch.node(e.to).health_check is None:
                    f.append(Finding("no_health_check", "low", e.to,
                                     f"{e.to} sits behind {n.id} with no health check; broken replicas keep getting traffic.",
                                     "Add a shallow health check."))

        # shards
        if n.type == "shard_router":
            shards = arch.out_edges(n.id)
            total_w = sum(e.weight for e in shards) or 1.0
            fair = 1 / len(shards)
            for e in shards:
                share = e.weight / total_w
                if len(shards) > 1 and share > 1.5 * fair:
                    f.append(Finding("shard_hotspot", "high", e.to,
                                     f"{e.to} owns {share:.0%} of {n.id}'s keys (even split would be {fair:.0%}); "
                                     "it saturates first and caps the whole cluster.",
                                     "Re-shard or split hot keys; use a hash of the key, not ranges by customer size."))

        # read replicas nobody reads from
        if n.read_replicas and not any(e.write_ratio is not None for e in arch.edges if e.to == n.id):
            f.append(Finding("replicas_unused", "low", n.id,
                             f"{n.id} has read replicas ({', '.join(n.read_replicas)}) but no caller sets a "
                             "write_ratio, so every read still hits the primary.",
                             "Send reads to the replicas (set write_ratio on the calling edges)."))

        # deep health checks
        hc = n.health_check
        if hc and hc.deep:
            deps = [e.to for e in arch.out_edges(n.id) if e.required]
            f.append(Finding("deep_health_check", "medium", n.id,
                             f"{n.id} health check fails when a dependency ({', '.join(deps)}) fails, so a "
                             "dependency outage makes the LB restart healthy replicas.",
                             "Use shallow (liveness) health checks; report dependency state separately."))

        # timeout inversion: callee keeps working after every caller gave up
        inbound = [e.timeout_ms for e in arch.edges if e.to == n.id]
        outbound = [worst_case_ms(arch, e) for e in arch.out_edges(n.id) if e.required]
        if inbound and outbound and sum(outbound) > max(inbound):
            f.append(Finding("timeout_inversion", "medium", n.id,
                             f"{n.id} may spend up to {sum(outbound) / 1000:.1f}s on downstream calls, "
                             f"but callers give up after {max(inbound) / 1000:.1f}s; that work is wasted under load.",
                             "Make downstream timeouts x retries fit inside the caller's timeout (deadline propagation)."))

    for e in arch.edges:
        if e.from_ not in live:
            continue
        r = e.retry
        tgt = f"{e.from_}->{e.to}"
        if r.attempts and r.backoff == "none":
            f.append(Finding("retry_no_backoff", "high", tgt,
                             f"{tgt} retries {r.attempts}x immediately. Under failure this multiplies load "
                             f"on {e.to} by up to {r.attempts + 1}x exactly when it is struggling.",
                             "Use exponential backoff with jitter, and a retry budget or circuit breaker."))
        elif r.attempts and not r.jitter:
            f.append(Finding("retry_no_jitter", "medium", tgt,
                             f"{tgt} retries without jitter; clients retry in sync (thundering herd).",
                             "Add jitter to the backoff."))
        if e.pool and arch.node(e.to).type != "external":
            cap = arch.node(e.to).capacity
            if e.pool > cap:
                f.append(Finding("pool_exceeds_capacity", "low", tgt,
                                 f"{tgt} pool ({e.pool}) is larger than {e.to}'s capacity ({cap}).",
                                 "Size pools to what the callee can actually serve."))

    # retry amplification along paths
    for node_id, amp, path in _retry_amplification(arch):
        if amp >= 8:
            f.append(Finding("retry_amplification", "high", node_id,
                             f"Retries multiply up to {amp}x on the way to {node_id} ({' -> '.join(path)}).",
                             "Retry at one layer only (usually the edge), not at every hop."))

    for cyc in startup_cycles(arch):
        f.append(Finding("startup_cycle", "critical", " -> ".join(cyc),
                         f"Startup dependency cycle: {' -> '.join(cyc)}. If these all go down, none can boot.",
                         "Break the cycle: let one side start without the other and connect later."))

    return sorted(f, key=lambda x: (SEVERITY_ORDER[x.severity], x.rule, x.target))


def _retry_amplification(arch: Architecture) -> list[tuple[str, int, list[str]]]:
    best: dict[str, tuple[int, list[str]]] = {}

    def walk(node_id: str, amp: int, path: list[str]) -> None:
        if node_id in path or len(path) > 30:
            return
        path = path + [node_id]
        if amp > best.get(node_id, (0, []))[0]:
            best[node_id] = (amp, path)
        for e in arch.out_edges(node_id):
            walk(e.to, amp * (e.retry.attempts + 1), path)

    for s in arch.traffic.sources:
        walk(s.target, 1, [])
    return [(n, a, p) for n, (a, p) in best.items() if len(p) > 2]
