"""Metrics collection during a run, and the summary computed after it."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

# latency histogram buckets ~2% wide, so windows can be merged cheaply
_LOG_RES = math.log(1.02)


def bucket(ms: float) -> int:
    return int(math.log1p(max(ms, 0.0)) / _LOG_RES)


def hist_quantile(hist: Counter, q: float) -> float:
    total = sum(hist.values())
    if not total:
        return 0.0
    rank = q * (total - 1)
    seen = 0
    for b in sorted(hist):
        seen += hist[b]
        if seen > rank:
            return math.expm1((b + 0.5) * _LOG_RES)
    return math.expm1((max(hist) + 0.5) * _LOG_RES)


def quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


@dataclass
class Second:
    """Raw counters for one simulated second; latencies are dropped once summarized."""

    total: int = 0
    ok: int = 0
    errors: Counter = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)


@dataclass
class SecondStats:
    t: int
    rps: int
    err_rate: float
    p50: float
    p99: float


@dataclass
class NodeSnapshot:
    t: int
    up: int
    capacity: int
    in_use: int
    queued: int


class Collector:
    def __init__(self) -> None:
        self._client: dict[int, Second] = defaultdict(Second)
        self._node: dict[str, dict[int, Second]] = defaultdict(lambda: defaultdict(Second))
        self.client_series: list[SecondStats] = []
        self.node_series: dict[str, list[SecondStats]] = defaultdict(list)
        self.snapshots: dict[str, list[NodeSnapshot]] = defaultdict(list)
        self.log: list[tuple[float, str, str, str]] = []  # (t, node, kind, detail)
        self.client_errors: Counter = Counter()
        self.node_errors: dict[str, Counter] = defaultdict(Counter)
        # per-second client latency histograms (successful requests), for windowed SLOs
        self.client_hist: dict[int, Counter] = {}

    # -- recording (called by the engine)

    def client(self, t: float, ok: bool, latency_ms: float, reason: str = "") -> None:
        s = self._client[int(t)]
        s.total += 1
        if ok:
            s.ok += 1
            s.latencies.append(latency_ms)
        else:
            s.errors[reason] += 1

    def node(self, node_id: str, t: float, ok: bool, latency_ms: float, reason: str = "") -> None:
        s = self._node[node_id][int(t)]
        s.total += 1
        if ok:
            s.ok += 1
            s.latencies.append(latency_ms)
        else:
            s.errors[reason] += 1

    def event(self, t: float, node_id: str, kind: str, detail: str = "") -> None:
        self.log.append((t, node_id, kind, detail))

    def snapshot(self, node_id: str, snap: NodeSnapshot) -> None:
        self.snapshots[node_id].append(snap)

    def close_second(self, t: int, measure_from: float) -> None:
        """Summarize second `t` and drop its raw latencies."""
        measured = t >= measure_from
        s = self._client.pop(t, None)
        if s is not None:
            self.client_series.append(self._stats(t, s))
            self.client_hist[t] = Counter(bucket(v) for v in s.latencies)
            if measured:
                self.client_errors.update(s.errors)
        for node_id, seconds in self._node.items():
            ns = seconds.pop(t, None)
            if ns is not None:
                self.node_series[node_id].append(self._stats(t, ns))
                if measured:
                    self.node_errors[node_id].update(ns.errors)

    @staticmethod
    def _stats(t: int, s: Second) -> SecondStats:
        lats = sorted(s.latencies)
        return SecondStats(
            t=t,
            rps=s.total,
            err_rate=(s.total - s.ok) / s.total if s.total else 0.0,
            p50=quantile(lats, 0.50),
            p99=quantile(lats, 0.99),
        )


# --------------------------------------------------------------------------- summary


@dataclass
class NodeSummary:
    id: str
    peak_util: float
    mean_util: float
    max_queued: int
    min_up: int
    replicas: int
    requests: int
    errors: dict[str, int]
    restarts: int


@dataclass
class RunSummary:
    architecture: str
    scenario: str
    status: str  # ok | degraded | slow_recovery | no_recovery
    passed: bool
    requests: int
    error_rate: float
    p50_ms: float
    p99_ms: float
    breach_seconds: int
    first_breach_s: float | None
    last_breach_s: float | None
    recovery_s: float | None  # seconds after the last event until back within SLO
    client_errors: dict[str, int]
    cascade: list[dict[str, Any]]
    nodes: list[NodeSummary]
    skipped_targets: list[str]
    wall_time_s: float = 0.0
    sim_events: int = 0

    def headline(self) -> str:
        bits = [f"{self.status.upper()}", f"err {self.error_rate:.2%}", f"p99 {self.p99_ms:.0f}ms"]
        if self.breach_seconds:
            bits.append(f"{self.breach_seconds}s in breach")
        if self.recovery_s is not None and self.status != "ok":
            bits.append(f"recovered after {self.recovery_s:.0f}s")
        return " | ".join(bits)


def summarize(
    col: Collector,
    *,
    architecture: str,
    scenario: str,
    measure_from: float,
    duration_s: float,
    last_event_s: float,
    slo_err: float,
    slo_p99: float,
    recover_within_s: float,
    window_s: int,
    replicas: dict[str, int],
    skipped_targets: list[str],
) -> RunSummary:
    start = int(measure_from)
    end = int(duration_s) - 2  # last second the ticker closed
    per_t = {s.t: s for s in col.client_series}

    # rolling-window SLO: a second is "in breach" if the window ending there breaks the SLO
    breaches: list[int] = []
    win_total = win_err = 0
    win_hist: Counter = Counter()
    overall_hist: Counter = Counter()
    total = errors = 0
    for t in range(start, end + 1):
        s = per_t.get(t)
        if s is not None:
            e = round(s.rps * s.err_rate)
            win_total += s.rps
            win_err += e
            total += s.rps
            errors += e
            h = col.client_hist.get(t, Counter())
            win_hist.update(h)
            overall_hist.update(h)
        old = t - window_s
        if old >= start and (o := per_t.get(old)) is not None:
            win_total -= o.rps
            win_err -= round(o.rps * o.err_rate)
            win_hist.subtract(col.client_hist.get(old, Counter()))
            win_hist = +win_hist  # drop zero/negative buckets
        if win_total == 0:
            breaches.append(t)  # nothing completing at all: requests are hanging
        elif win_err / win_total > slo_err or hist_quantile(win_hist, 0.99) > slo_p99:
            breaches.append(t)

    tail = 30  # the last 30s of the run must be clean to call it recovered
    if not breaches:
        status, recovery = "ok", None
    elif breaches[-1] >= end - tail:
        status, recovery = "no_recovery", None
    else:
        recovery = max(0.0, breaches[-1] + 1 - last_event_s)
        status = "degraded" if recovery <= recover_within_s else "slow_recovery"

    cascade, seen_nodes = [], set()
    for t, node_id, kind, detail in sorted(col.log):
        if kind in {"unhealthy", "down", "boot_blocked", "saturated"} and (node_id, kind) not in seen_nodes:
            seen_nodes.add((node_id, kind))
            cascade.append({"t": round(t, 1), "node": node_id, "what": kind, "detail": detail})

    restarts = Counter(n for _, n, k, _ in col.log if k == "unhealthy")
    nodes = []
    for node_id, snaps in col.snapshots.items():
        snaps = [s for s in snaps if s.t >= measure_from]
        utils = [s.in_use / s.capacity if s.capacity else 1.0 for s in snaps]
        reqs = sum(s.rps for s in col.node_series.get(node_id, []) if s.t >= measure_from)
        nodes.append(
            NodeSummary(
                id=node_id,
                peak_util=max(utils, default=0.0),
                mean_util=sum(utils) / len(utils) if utils else 0.0,
                max_queued=max((s.queued for s in snaps), default=0),
                min_up=min((s.up for s in snaps), default=0),
                replicas=replicas[node_id],
                requests=reqs,
                errors=dict(col.node_errors.get(node_id, {})),
                restarts=restarts.get(node_id, 0),
            )
        )

    return RunSummary(
        architecture=architecture,
        scenario=scenario,
        status=status,
        passed=status in {"ok", "degraded"},
        requests=total,
        error_rate=errors / total if total else 0.0,
        p50_ms=hist_quantile(overall_hist, 0.50),
        p99_ms=hist_quantile(overall_hist, 0.99),
        breach_seconds=len(breaches),
        first_breach_s=float(breaches[0]) if breaches else None,
        last_breach_s=float(breaches[-1]) if breaches else None,
        recovery_s=recovery,
        client_errors=dict(col.client_errors),
        cascade=cascade,
        nodes=nodes,
        skipped_targets=skipped_targets,
    )
