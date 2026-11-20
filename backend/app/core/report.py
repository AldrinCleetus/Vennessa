"""Markdown reports for runs, suites, analysis, and comparisons."""

from __future__ import annotations

from .analyze import Finding
from .metrics import RunSummary

STATUS_MEANING = {
    "ok": "stayed within SLO the whole time",
    "degraded": "breached SLO but recovered in time",
    "slow_recovery": "recovered, but slower than the SLO allows",
    "no_recovery": "still failing at the end of the run (did not self-heal)",
}


def _fmt_s(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}s"


def run_markdown(s: RunSummary) -> str:
    lines = [
        f"## {s.scenario} on `{s.architecture}`",
        "",
        f"**{s.status.upper()}**: {STATUS_MEANING[s.status]}.",
        "",
        "| Requests | Error rate | p50 | p99 | Seconds in breach | First breach | Recovery after last event |",
        "|---|---|---|---|---|---|---|",
        f"| {s.requests:,} | {s.error_rate:.2%} | {s.p50_ms:.0f} ms | {s.p99_ms:.0f} ms | "
        f"{s.breach_seconds} | {_fmt_s(s.first_breach_s)} | {_fmt_s(s.recovery_s)} |",
        "",
    ]
    if s.skipped_targets:
        lines += [f"Skipped events for missing nodes: {', '.join(s.skipped_targets)}", ""]
    if s.cascade:
        lines += ["### Failure timeline", "", "| t | Node | What | Detail |", "|---|---|---|---|"]
        lines += [f"| {c['t']}s | {c['node']} | {c['what']} | {c['detail']} |" for c in s.cascade[:40]]
        lines.append("")
    if s.client_errors:
        lines += ["### Client errors by cause", "", "| Cause | Count |", "|---|---|"]
        for reason, count in sorted(s.client_errors.items(), key=lambda x: -x[1])[:15]:
            lines.append(f"| {reason} | {count:,} |")
        lines.append("")
    lines += [
        "### Nodes",
        "",
        "| Node | Peak util | Mean util | Max queued | Min up / replicas | Restarts | Errors |",
        "|---|---|---|---|---|---|---|",
    ]
    for n in sorted(s.nodes, key=lambda n: -n.peak_util):
        errs = sum(n.errors.values())
        lines.append(
            f"| {n.id} | {n.peak_util:.0%} | {n.mean_util:.0%} | {n.max_queued} | "
            f"{n.min_up}/{n.replicas} | {n.restarts} | {errs:,} |"
        )
    lines += ["", f"_Simulated {s.sim_events:,} node requests in {s.wall_time_s:.1f}s wall time._", ""]
    return "\n".join(lines)


def suite_markdown(arch_name: str, runs: list[RunSummary]) -> str:
    lines = [
        f"# Scenario suite: `{arch_name}`",
        "",
        "| Scenario | Status | Error rate | p99 | In breach | Recovery |",
        "|---|---|---|---|---|---|",
    ]
    for s in runs:
        lines.append(
            f"| {s.scenario} | {s.status} | {s.error_rate:.2%} | {s.p99_ms:.0f} ms | "
            f"{s.breach_seconds}s | {_fmt_s(s.recovery_s)} |"
        )
    lines.append("")
    for s in runs:
        lines.append(run_markdown(s))
    return "\n".join(lines)


def findings_markdown(arch_name: str, findings: list[Finding]) -> str:
    lines = [f"# Weak spots: `{arch_name}`", ""]
    if not findings:
        return "\n".join(lines + ["No findings.", ""])
    lines += ["| Severity | Rule | Target | Finding | Suggested fix |", "|---|---|---|---|---|"]
    for f in findings:
        lines.append(f"| {f.severity} | {f.rule} | {f.target} | {f.message} | {f.fix} |")
    return "\n".join(lines + [""])
