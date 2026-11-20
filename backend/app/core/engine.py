"""Discrete-event simulation of an Architecture under a Scenario.

Interface (the swap point for a faster engine later):

    run(arch, scenario) -> RunSummary

Behaviors modeled:
  - Poisson traffic per source, shaped by the scenario multiplier
  - per-node worker slots (replicas x concurrency) and bounded queues -> rejections
  - lognormal latency from p50/p99, optional slow-down factor
  - synchronous calls with timeouts; the callee keeps working after the caller gives up
  - retries with none / fixed / exponential backoff and optional jitter
  - per-edge connection pools
  - caches with warm/cold hit rate; restarts reset warmth (cold-start stampede)
  - external rate limits (token bucket) -> 429s
  - load balancers route to backends with healthy replicas only
  - shard routers send each request to the shard that owns its key (no failover:
    a down shard fails its share of keys)
  - datastore read replicas: reads go to a healthy replica, writes to the primary
  - health checks pull and restart replicas after repeated failures (cascades)
  - startup sequencing: a booting replica waits for everything in `requires` (restart deadlocks)
  - kill / restore / restart / slow / rate_limit events
"""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field

import simpy

from .metrics import Collector, NodeSnapshot, RunSummary, summarize
from .model import Architecture, Edge, Node
from .scenario import Event, Scenario

FAST_FAIL_S = 0.001  # connection refused / immediate rejection
HANG_S = 3600.0  # blackholed requests never answer within a run


class Pool:
    """Counting semaphore with a bounded queue and adjustable capacity."""

    def __init__(self, env: simpy.Environment, capacity: int, queue_max: int | None) -> None:
        self.env = env
        self.capacity = capacity
        self.queue_max = queue_max
        self.in_use = 0
        self.waiters: deque[simpy.Event] = deque()

    def acquire(self) -> simpy.Event | None:
        """Returns an event that succeeds with True (slot granted) or False (rejected),
        or None if rejected right away because the queue is full."""
        if self.in_use < self.capacity and not self.waiters:
            self.in_use += 1
            ev = self.env.event()
            ev.succeed(True)
            return ev
        if self.queue_max is not None and len(self.waiters) >= self.queue_max:
            return None
        ev = self.env.event()
        self.waiters.append(ev)
        return ev

    def release(self) -> None:
        self.in_use -= 1
        self._grant()

    def resize(self, capacity: int, queue_max: int | None) -> None:
        self.capacity, self.queue_max = capacity, queue_max
        if capacity == 0:
            while self.waiters:
                self.waiters.popleft().succeed(False)
            return
        self._grant()
        if queue_max is not None:
            while len(self.waiters) > queue_max:
                self.waiters.pop().succeed(False)

    def _grant(self) -> None:
        while self.waiters and self.in_use < self.capacity:
            self.in_use += 1
            self.waiters.popleft().succeed(True)


@dataclass
class Boot:
    count: int
    cancelled: bool = False


@dataclass
class NodeState:
    node: Node
    pool: Pool
    up: int
    killed: int = 0
    blackhole: bool = False
    slow_factor: float = 1.0
    rate_limit: float | None = None
    tokens: float = 0.0
    tokens_at: float = 0.0
    warm_since: float = 0.0
    boots: list[Boot] = field(default_factory=list)
    # health check window
    hc_total: int = 0
    hc_failed: int = 0
    hc_streak: int = 0
    saturated_logged: bool = False

    @property
    def booting(self) -> int:
        return sum(b.count for b in self.boots if not b.cancelled)


class Simulation:
    def __init__(self, arch: Architecture, scenario: Scenario) -> None:
        self.arch = arch
        self.sc = scenario
        self.env = simpy.Environment()
        self.rng = random.Random(scenario.seed)
        # separate stream so adding shard keys doesn't shift any other random draw
        self.key_rng = random.Random(f"keys-{scenario.seed}")
        self.col = Collector()
        self.out: dict[str, list[Edge]] = {n.id: arch.out_edges(n.id) for n in arch.nodes}
        self.edge_pools: dict[tuple[str, str], Pool] = {
            e.key: Pool(self.env, e.pool, None) for e in arch.edges if e.pool
        }
        self.st: dict[str, NodeState] = {}
        for n in arch.nodes:
            self.st[n.id] = NodeState(
                node=n,
                pool=Pool(self.env, n.capacity, n.replicas * n.queue_per_replica),
                up=n.replicas,
                rate_limit=n.rate_limit_rps,
                tokens=n.rate_limit_rps or 0.0,
                warm_since=-n.warmup_s,  # caches start warm (steady state)
            )
        self.skipped = scenario.missing_targets(arch)
        self.requests_handled = 0

    # ------------------------------------------------------------------ run

    def run(self) -> RunSummary:
        t0 = time.perf_counter()
        for src in self.arch.traffic.sources:
            self.env.process(self._source(src))
        for ev in sorted(self.sc.events, key=lambda e: e.at_s):
            if ev.target not in self.skipped:
                self.env.process(self._event(ev))
        for ns in self.st.values():
            if ns.node.health_check:
                self.env.process(self._health_check(ns))
        self.env.process(self._ticker())
        self.env.run(until=self.sc.duration_s)
        wall = time.perf_counter() - t0

        slo = self.sc.effective_slo(self.arch)
        s = summarize(
            self.col,
            architecture=self.arch.name,
            scenario=self.sc.name,
            measure_from=self.sc.warmup_s,
            duration_s=self.sc.duration_s,
            last_event_s=self.sc.last_event_s,
            slo_err=slo.error_rate_max,
            slo_p99=slo.p99_ms_max,
            recover_within_s=slo.recover_within_s,
            window_s=slo.window_s,
            replicas={n.id: n.replicas for n in self.arch.nodes},
            skipped_targets=self.skipped,
        )
        s.wall_time_s = wall
        s.sim_events = self.requests_handled
        return s

    # ------------------------------------------------------------------ traffic

    def _source(self, src):
        shape = self.sc.traffic
        applies = shape.sources is None or src.name in shape.sources
        while True:
            mult = shape.multiplier_at(self.env.now) if applies else 1.0
            rate = src.rps * mult
            if rate <= 0:
                yield self.env.timeout(1.0)
                continue
            yield self.env.timeout(self.rng.expovariate(rate))
            self.env.process(self._client(src))

    def _client(self, src):
        start = self.env.now
        key = self.key_rng.random()  # e.g. the customer/account id this request is for
        proc = self.env.process(self._handle(src.target, src.name, key))
        timer = self.env.timeout(src.timeout_ms / 1000)
        yield proc | timer
        lat = (self.env.now - start) * 1000
        if proc.triggered:
            ok, reason = proc.value
            self.col.client(self.env.now, ok, lat, reason)
        else:
            self.col.client(self.env.now, False, lat, "client_timeout")

    # ------------------------------------------------------------------ request handling

    def _handle(self, node_id: str, src: str, key: float):
        """Process a request (from traffic source `src`, for shard key `key`) at a node.
        Returns (ok, reason)."""
        self.requests_handled += 1
        ns = self.st[node_id]
        n = ns.node
        start = self.env.now

        def done(ok: bool, reason: str = "", own: bool = False):
            self.col.node(node_id, self.env.now, ok, (self.env.now - start) * 1000, reason)
            ns.hc_total += 1
            if not ok and (own or (n.health_check and n.health_check.deep)):
                ns.hc_failed += 1
            return ok, reason

        if ns.up == 0:
            if ns.blackhole:
                yield self.env.timeout(HANG_S)
            else:
                yield self.env.timeout(FAST_FAIL_S)
            return done(False, f"{node_id}:down", own=True)

        if ns.rate_limit is not None and not self._take_token(ns):
            yield self.env.timeout(FAST_FAIL_S)
            return done(False, f"{node_id}:rate_limited", own=True)

        slot = ns.pool.acquire()
        if slot is None:
            yield self.env.timeout(FAST_FAIL_S)
            self._mark_saturated(ns)
            return done(False, f"{node_id}:queue_full", own=True)
        granted = yield slot
        if not granted:
            return done(False, f"{node_id}:dropped", own=True)

        released = False
        try:
            yield self.env.timeout(self._service_time(n) * ns.slow_factor)
            if not n.holds_slot_during_calls:
                ns.pool.release()
                released = True

            if n.type == "load_balancer":
                ok, reason = yield from self._route(node_id, src, key)
            elif n.type == "shard_router":
                ok, reason = yield from self._shard(node_id, src, key)
            else:
                ok, reason = True, ""
                for e in self.out[node_id]:
                    if e.sources is not None and src not in e.sources:
                        continue
                    if e.p < 1.0 and self.rng.random() >= e.p:
                        continue
                    call_ok, call_reason = yield from self._call(e, src, key)
                    if not call_ok and e.required:
                        ok, reason = False, call_reason
                        break
        finally:
            if not released:
                ns.pool.release()
        return done(ok, reason)

    def _route(self, lb_id: str, src: str, key: float):
        routes = [e for e in self.out[lb_id] if e.sources is None or src in e.sources]
        backends = [e for e in routes if self.st[e.to].up > 0]
        if not backends:
            yield self.env.timeout(FAST_FAIL_S)
            return False, f"{lb_id}:no_healthy_backend"
        e = self.rng.choices(backends, weights=[b.weight for b in backends])[0]
        return (yield from self._call(e, src, key))

    def _shard(self, router_id: str, src: str, key: float):
        """Send the request to the shard owning `key`. Edge weights are each shard's share
        of the key space. Unlike a load balancer, a down shard is not skipped."""
        routes = [e for e in self.out[router_id] if e.sources is None or src in e.sources]
        if not routes:
            yield self.env.timeout(FAST_FAIL_S)
            return False, f"{router_id}:no_route"
        total = sum(e.weight for e in routes) or 1.0
        acc = 0.0
        chosen = routes[-1]
        for e in routes:
            acc += e.weight / total
            if key < acc:
                chosen = e
                break
        return (yield from self._call(chosen, src, key))

    def _read_target(self, e: Edge) -> str | None:
        """Where a read on edge `e` goes: a healthy replica of `e.to`, else the primary
        (if read_fallback), else None."""
        primary = self.st[e.to].node
        healthy = [r for r in primary.read_replicas if self.st[r].up > 0]
        if healthy:
            return self.rng.choice(healthy)
        return e.to if primary.read_fallback else None

    def _call(self, e: Edge, src: str, key: float):
        """Call along an edge with cache lookup, pool, timeout and retries."""
        if e.via_cache:
            hit = yield from self._cache_lookup(e, src, key)
            if hit:
                return True, ""

        # read/write split: decided once per call, the replica is re-picked on each retry
        splits = e.write_ratio is not None and bool(self.st[e.to].node.read_replicas)
        is_read = splits and self.rng.random() >= e.write_ratio

        last = f"{e.to}:error"
        for attempt in range(e.retry.attempts + 1):
            if attempt:
                yield self.env.timeout(self._backoff(e, attempt))

            pool = self.edge_pools.get(e.key)
            deadline = self.env.now + e.timeout_ms / 1000
            if pool is not None:
                slot = pool.acquire()
                res = yield slot | self.env.timeout(e.timeout_ms / 1000)
                if slot not in res:
                    # gave up waiting for a connection; release it if it arrives later
                    slot.callbacks.append(lambda ev, p=pool: p.release() if ev.value else None)
                    last = f"{e.from_}->{e.to}:pool_timeout"
                    continue

            target = self._read_target(e) if is_read else e.to
            if target is None:
                if pool is not None:
                    pool.release()
                yield self.env.timeout(FAST_FAIL_S)
                last = f"{e.to}:no_read_replica"
                continue
            proc = self.env.process(self._handle(target, src, key))
            remaining = max(0.0, deadline - self.env.now)
            res = yield proc | self.env.timeout(remaining)
            if pool is not None:
                if proc.triggered:
                    pool.release()
                else:
                    # the connection stays busy until the callee actually finishes
                    proc.callbacks.append(lambda _ev, p=pool: p.release())
            if proc in res:
                ok, reason = proc.value
                if ok:
                    return True, ""
                last = reason
            else:
                last = f"{e.from_}->{e.to}:timeout"
        return False, last

    def _cache_lookup(self, e: Edge, src: str, key: float):
        cache = self.st[e.via_cache]
        proc = self.env.process(self._handle(e.via_cache, src, key))
        res = yield proc | self.env.timeout(e.cache_timeout_ms / 1000)
        if proc not in res or not proc.value[0]:
            return False  # cache unavailable or slow: treat as a miss
        return self.rng.random() < self._hit_rate(cache)

    # ------------------------------------------------------------------ helpers

    def _service_time(self, n: Node) -> float:
        lat = n.latency_ms
        ms = self.rng.lognormvariate(lat.mu, lat.sigma)
        return min(ms, lat.p99 * 5) / 1000

    def _backoff(self, e: Edge, attempt: int) -> float:
        r = e.retry
        if r.backoff == "none":
            ms = 0.0
        elif r.backoff == "fixed":
            ms = r.base_ms
        else:
            ms = min(r.max_ms, r.base_ms * 2 ** (attempt - 1))
        if r.jitter:
            ms = self.rng.uniform(0, ms)
        return ms / 1000

    def _hit_rate(self, ns: NodeState) -> float:
        n = ns.node
        age = self.env.now - ns.warm_since
        frac = 1.0 if n.warmup_s <= 0 else min(1.0, max(0.0, age / n.warmup_s))
        return n.cold_hit_rate + (n.warm_hit_rate - n.cold_hit_rate) * frac

    def _take_token(self, ns: NodeState) -> bool:
        rate = ns.rate_limit or 0.0
        now = self.env.now
        ns.tokens = min(rate, ns.tokens + (now - ns.tokens_at) * rate)
        ns.tokens_at = now
        if ns.tokens >= 1:
            ns.tokens -= 1
            return True
        return False

    def _mark_saturated(self, ns: NodeState) -> None:
        if not ns.saturated_logged:
            ns.saturated_logged = True
            self.col.event(self.env.now, ns.node.id, "saturated", "queue full, rejecting requests")

    def _set_up(self, ns: NodeState, up: int, why: str) -> None:
        was = ns.up
        ns.up = max(0, up)
        n = ns.node
        ns.pool.resize(ns.up * n.concurrency_per_replica, ns.up * n.queue_per_replica)
        if was > 0 and ns.up == 0:
            self.col.event(self.env.now, n.id, "down", why)
        if was == 0 and ns.up > 0:
            self.col.event(self.env.now, n.id, "up", why)
            if n.type == "cache":
                ns.warm_since = self.env.now  # empty after coming back

    # ------------------------------------------------------------------ lifecycle

    def _boot(self, ns: NodeState, count: int, why: str):
        b = Boot(count)
        ns.boots.append(b)
        yield self.env.timeout(ns.node.startup.time_s)
        blocked_logged = False
        while not b.cancelled:
            missing = [r for r in ns.node.startup.requires if self.st[r].up == 0]
            if not missing:
                break
            if not blocked_logged:
                self.col.event(self.env.now, ns.node.id, "boot_blocked", f"waiting on {', '.join(missing)}")
                blocked_logged = True
            yield self.env.timeout(1.0)
        ns.boots.remove(b)
        if not b.cancelled:
            self._set_up(ns, ns.up + count, why)

    def _kill(self, ns: NodeState, count: int, why: str) -> int:
        """Kill up to `count` replicas (running first, then booting). Returns how many."""
        killed = 0
        take = min(count, ns.up)
        if take:
            self._set_up(ns, ns.up - take, why)
            killed += take
        for b in ns.boots:
            if killed >= count:
                break
            if not b.cancelled:
                b.cancelled = True
                killed += b.count
        ns.killed += killed
        return killed

    def _health_check(self, ns: NodeState):
        hc = ns.node.health_check
        while True:
            yield self.env.timeout(hc.interval_s)
            total, failed = ns.hc_total, ns.hc_failed
            ns.hc_total = ns.hc_failed = 0
            if total and failed / total > hc.fail_threshold and ns.up > 0:
                ns.hc_streak += 1
            else:
                ns.hc_streak = 0
            if ns.hc_streak >= hc.unhealthy_after:
                ns.hc_streak = 0
                self.col.event(
                    self.env.now, ns.node.id, "unhealthy",
                    f"{failed}/{total} failed; restarting 1 of {ns.up} replicas",
                )
                self._set_up(ns, ns.up - 1, "health check restart")
                self.env.process(self._boot(ns, 1, "health check restart"))

    def _event(self, ev: Event):
        yield self.env.timeout(ev.at_s)
        ns = self.st[ev.target]
        n = ns.node
        if ev.action in {"kill", "restart"}:
            ns.blackhole = ev.mode == "blackhole"
            count = ev.replicas or n.replicas
            killed = self._kill(ns, count, f"{ev.action} event")
            self.col.event(self.env.now, n.id, "injected", f"{ev.action} {killed} replica(s)")
            if ev.action == "restart":
                ns.killed -= killed
                self.env.process(self._boot(ns, killed, "restart event"))
        elif ev.action == "restore":
            count = min(ev.replicas or ns.killed, ns.killed)
            ns.killed -= count
            ns.blackhole = False
            self.col.event(self.env.now, n.id, "injected", f"restore {count} replica(s)")
            self.env.process(self._boot(ns, count, "restore event"))
        elif ev.action == "slow":
            self.col.event(self.env.now, n.id, "injected", f"slow x{ev.factor}")
            ns.slow_factor = ev.factor
            if ev.duration_s:
                yield self.env.timeout(ev.duration_s)
                ns.slow_factor = 1.0
                self.col.event(self.env.now, n.id, "injected", "slow ended")
        elif ev.action == "rate_limit":
            prev = ns.rate_limit
            self.col.event(self.env.now, n.id, "injected", f"rate limit {ev.rps} rps")
            ns.rate_limit = ev.rps
            if ev.duration_s:
                yield self.env.timeout(ev.duration_s)
                ns.rate_limit = prev
                self.col.event(self.env.now, n.id, "injected", "rate limit ended")

    def _ticker(self):
        t = 0
        while True:
            yield self.env.timeout(1.0)
            for node_id, ns in self.st.items():
                self.col.snapshot(
                    node_id,
                    NodeSnapshot(t=t, up=ns.up, capacity=ns.pool.capacity,
                                 in_use=ns.pool.in_use, queued=len(ns.pool.waiters)),
                )
            self.col.close_second(t, self.sc.warmup_s)
            t += 1


def run(arch: Architecture, scenario: Scenario) -> RunSummary:
    return Simulation(arch, scenario).run()
