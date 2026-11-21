"""Scenarios: traffic shape + timed failure events, defined in data files.

    name: redis_restart_at_peak
    duration_s: 1800
    warmup_s: 300          # ignore metrics before this (lets caches warm)
    seed: 42
    traffic:
      multiplier: 1.0      # applied to every source
      ramp:                # piecewise-linear multiplier over time
        - {at_s: 600, multiplier: 2.5}
    events:
      - {at_s: 900, action: kill, target: redis}
      - {at_s: 960, action: restore, target: redis}
    slo: {error_rate_max: 0.01}   # optional: overrides the architecture's SLO

Actions:
  kill      target down. `replicas: n` kills only n replicas. `mode: refuse` (fast error, default)
            or `blackhole` (requests hang until the caller's timeout).
  restore   bring killed replicas back; they go through startup (time + requires).
  restart   kill + immediate restore (cold restart).
  slow      multiply target latency by `factor` (until `duration_s` passes, or forever).
  rate_limit  set an external's rate limit to `rps` (until `duration_s`).

Targets that don't exist in a given architecture are reported and skipped, so the
same scenario folder can be run against different architectures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from .model import SLO, Architecture, Strict, read_data


class RampPoint(Strict):
    at_s: float
    multiplier: float
    # transition time from the previous multiplier; 0 = step change
    ramp_s: float = 0.0


class TrafficShape(Strict):
    multiplier: float = 1.0
    ramp: list[RampPoint] = Field(default_factory=list)
    # restrict the multiplier to these source names (default: all)
    sources: list[str] | None = None

    def multiplier_at(self, t: float) -> float:
        m = self.multiplier
        for pt in sorted(self.ramp, key=lambda r: r.at_s):
            if t < pt.at_s:
                break
            if pt.ramp_s > 0 and t < pt.at_s + pt.ramp_s:
                return m + (pt.multiplier - m) * (t - pt.at_s) / pt.ramp_s
            m = pt.multiplier
        return m


class Event(Strict):
    at_s: float
    action: Literal["kill", "restore", "restart", "slow", "rate_limit"]
    target: str
    replicas: int | None = None
    mode: Literal["refuse", "blackhole"] = "refuse"
    factor: float = 1.0
    rps: float | None = None
    duration_s: float | None = None


class Scenario(Strict):
    name: str
    description: str = ""
    duration_s: float = 1800.0
    warmup_s: float = 300.0
    seed: int = 42
    traffic: TrafficShape = Field(default_factory=TrafficShape)
    events: list[Event] = Field(default_factory=list)
    slo: dict | None = None

    def effective_slo(self, arch: Architecture) -> SLO:
        base = arch.slo.model_dump()
        base.update(self.slo or {})
        return SLO.model_validate(base)

    def missing_targets(self, arch: Architecture) -> list[str]:
        return sorted({e.target for e in self.events if not arch.has_node(e.target)})

    @property
    def last_event_s(self) -> float:
        ends = [e.at_s + (e.duration_s or 0) for e in self.events]
        ends += [r.at_s + r.ramp_s for r in self.traffic.ramp]
        return max(ends, default=self.warmup_s)

    @property
    def fault_windows(self) -> list[tuple[float, float]]:
        """Injected-fault intervals used for windowed scoring and honest verdicts."""
        return [
            (float(e.at_s), float(e.at_s) + float(e.duration_s or 30.0))
            for e in self.events
        ]


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    data = read_data(path)
    data.setdefault("name", path.stem)
    return Scenario.model_validate(data)


def load_scenarios(path: str | Path) -> list[Scenario]:
    path = Path(path)
    if path.is_file():
        return [load_scenario(path)]
    files = sorted(p for p in path.iterdir() if p.suffix in {".yaml", ".yml", ".json"})
    return [load_scenario(p) for p in files]
