"""Compare two architectures: same scenarios, same seeds, side by side."""

from __future__ import annotations

from dataclasses import dataclass

from .analyze import Finding, analyze, estimate_load
from .engine import run
from .metrics import RunSummary
from .model import Architecture
from .report import _fmt_s, run_markdown
from .scenario import Scenario

RANK = {"ok": 0, "degraded": 1, "slow_recovery": 2, "no_recovery": 3}


@dataclass
class Comparison:
    a: Architecture
    b: Architecture
    runs_a: list[RunSummary]
    runs_b: list[RunSummary]
    new_findings: list[Finding]
    resolved_findings: list[Finding]
    util_rows: list[tuple[str, float | None, float | None]]

    @property
    def regressions(self) -> list[tuple[RunSummary, RunSummary]]:
        return [(x, y) for x, y in zip(self.runs_a, self.runs_b) if RANK[y.status] > RANK[x.status]]

    @property
    def improvements(self) -> list[tuple[RunSummary, RunSummary]]:
        return [(x, y) for x, y in zip(self.runs_a, self.runs_b) if RANK[y.status] < RANK[x.status]]


def compare(a: Architecture, b: Architecture, scenarios: list[Scenario], on_run=None) -> Comparison:
    runs_a, runs_b = [], []
    for sc in scenarios:
        for arch, out in ((a, runs_a), (b, runs_b)):
            s = run(arch, sc)
            out.append(s)
            if on_run:
                on_run(s)

    fa, fb = analyze(a), analyze(b)
    ka, kb = {f.key for f in fa}, {f.key for f in fb}
    new = [f for f in fb if f.key not in ka]
    resolved = [f for f in fa if f.key not in kb]

    la, lb = estimate_load(a).util, estimate_load(b).util
    ids = sorted(set(la) | set(lb), key=lambda i: -(lb.get(i) or la.get(i) or 0))
    util_rows = [(i, la.get(i), lb.get(i)) for i in ids]
    return Comparison(a, b, runs_a, runs_b, new, resolved, util_rows)


def comparison_markdown(c: Comparison) -> str:
    a, b = c.a.name, c.b.name
    lines = [
        f"# `{a}` vs `{b}`",
        "",
        f"- Scenarios: {len(c.runs_a)}",
        f"- Got worse in `{b}`: **{len(c.regressions)}**",
        f"- Got better in `{b}`: **{len(c.improvements)}**",
        f"- New weak spots in `{b}`: **{len(c.new_findings)}**; resolved: **{len(c.resolved_findings)}**",
        "",
        "## Scenarios",
        "",
        f"| Scenario | {a} | {b} | Error rate (A to B) | p99 (A to B) | Recovery (A to B) | Change |",
        "|---|---|---|---|---|---|---|",
    ]
    for x, y in zip(c.runs_a, c.runs_b):
        change = "worse" if RANK[y.status] > RANK[x.status] else "better" if RANK[y.status] < RANK[x.status] else ""
        lines.append(
            f"| {x.scenario} | {x.status} | {y.status} | {x.error_rate:.2%} to {y.error_rate:.2%} | "
            f"{x.p99_ms:.0f} to {y.p99_ms:.0f} ms | {_fmt_s(x.recovery_s)} to {_fmt_s(y.recovery_s)} | {change} |"
        )
    lines.append("")

    if c.new_findings:
        lines += [f"## New weak spots in `{b}`", "", "| Severity | Rule | Target | Finding |", "|---|---|---|---|"]
        lines += [f"| {f.severity} | {f.rule} | {f.target} | {f.message} |" for f in c.new_findings]
        lines.append("")
    if c.resolved_findings:
        lines += [f"## Resolved in `{b}`", "", "| Severity | Rule | Target |", "|---|---|---|"]
        lines += [f"| {f.severity} | {f.rule} | {f.target} |" for f in c.resolved_findings]
        lines.append("")

    lines += ["## Estimated baseline utilization", "", f"| Node | {a} | {b} |", "|---|---|---|"]
    pct = lambda v: "-" if v is None else f"{v:.0%}"  # noqa: E731
    lines += [f"| {i} | {pct(x)} | {pct(y)} |" for i, x, y in c.util_rows]
    lines.append("")

    if c.regressions:
        lines += [f"## Details: scenarios that got worse in `{b}`", ""]
        for _, y in c.regressions:
            lines.append(run_markdown(y))
    return "\n".join(lines)
