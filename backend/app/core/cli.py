"""archsim command line.

    archsim validate <arch>                  check a file (and show the composed result)
    archsim analyze  <arch>                  static weak-spot report
    archsim run      <arch> <scenario>       one simulation
    archsim suite    <arch> [scenarios/]     every scenario in a folder
    archsim compare  <archA> <archB> [scenarios/]   before vs after (merge / add component)

Architecture and scenario files are YAML or JSON. Reports go to ./reports/.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from .analyze import analyze as analyze_arch
from .analyze import estimate_load
from .compare import comparison_markdown
from .compare import compare as compare_archs
from .engine import run as run_sim
from .metrics import RunSummary
from .model import load_architecture
from .report import findings_markdown, run_markdown, suite_markdown
from .scenario import load_scenario, load_scenarios

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Simulate architectures under load and failure.")
console = Console()

STATUS_STYLE = {"ok": "green", "degraded": "yellow", "slow_recovery": "dark_orange", "no_recovery": "red"}
SEV_STYLE = {"critical": "bold red", "high": "red", "medium": "yellow", "low": "dim"}
DEFAULT_SCENARIOS = Path("scenarios")


def _write(report_dir: Path, name: str, text: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = report_dir / f"{stamp}_{name}.md"
    path.write_text(text)
    return path


def _progress(s: RunSummary) -> None:
    style = STATUS_STYLE[s.status]
    skipped = f" [dim](skipped: {', '.join(s.skipped_targets)})[/dim]" if s.skipped_targets else ""
    console.print(
        f"  {s.architecture:<28} {s.scenario:<28} [{style}]{s.headline()}[/{style}]"
        f" [dim]{s.wall_time_s:.1f}s[/dim]{skipped}"
    )


@app.command()
def validate(arch: Path, show: bool = typer.Option(False, help="Print the fully composed architecture.")):
    """Load and validate an architecture file (resolves include/unify/override)."""
    a = load_architecture(arch)
    console.print(f"[green]valid[/green] {a.name}: {len(a.nodes)} nodes, {len(a.edges)} edges, "
                  f"{len(a.traffic.sources)} traffic sources")
    if show:
        console.print(yaml.safe_dump(a.to_dict(), sort_keys=False))


@app.command()
def analyze(arch: Path, report_dir: Path = Path("reports")):
    """Static analysis: weak spots found from the model alone."""
    a = load_architecture(arch)
    findings = analyze_arch(a)
    t = Table(title=f"Weak spots: {a.name}", show_lines=False)
    for col in ("Severity", "Rule", "Target", "Finding"):
        t.add_column(col, overflow="fold")
    for f in findings:
        t.add_row(f"[{SEV_STYLE[f.severity]}]{f.severity}[/]", f.rule, f.target, f.message)
    console.print(t)

    load = estimate_load(a)
    u = Table(title="Estimated baseline load")
    for col in ("Node", "rps", "hold ms", "util"):
        u.add_column(col, justify="right" if col != "Node" else "left")
    for n in sorted(a.nodes, key=lambda n: -load.util[n.id]):
        u.add_row(n.id, f"{load.rps[n.id]:.0f}", f"{load.hold_ms[n.id]:.0f}", f"{load.util[n.id]:.0%}")
    console.print(u)
    console.print(f"report: {_write(report_dir, f'analyze_{a.name}', findings_markdown(a.name, findings))}")


@app.command()
def run(arch: Path, scenario: Path, report_dir: Path = Path("reports"),
        json_out: bool = typer.Option(False, "--json", help="Print the summary as JSON.")):
    """Run one scenario against one architecture."""
    a = load_architecture(arch)
    sc = load_scenario(scenario)
    s = run_sim(a, sc)
    if json_out:
        print(json.dumps(asdict(s), indent=2, default=str))
        return
    _progress(s)
    console.print(f"report: {_write(report_dir, f'run_{a.name}_{sc.name}', run_markdown(s))}")


@app.command()
def suite(arch: Path, scenarios: Path = DEFAULT_SCENARIOS, report_dir: Path = Path("reports")):
    """Run every scenario in a folder against one architecture."""
    a = load_architecture(arch)
    runs = []
    for sc in load_scenarios(scenarios):
        s = run_sim(a, sc)
        _progress(s)
        runs.append(s)
    failed = [s for s in runs if not s.passed]
    console.print(f"\n{len(runs) - len(failed)}/{len(runs)} passed")
    console.print(f"report: {_write(report_dir, f'suite_{a.name}', suite_markdown(a.name, runs))}")


@app.command()
def compare(arch_a: Path, arch_b: Path, scenarios: Path = DEFAULT_SCENARIOS, report_dir: Path = Path("reports")):
    """Run the same scenarios against two architectures and diff the results."""
    a, b = load_architecture(arch_a), load_architecture(arch_b)
    c = compare_archs(a, b, load_scenarios(scenarios), on_run=_progress)

    t = Table(title=f"{a.name} vs {b.name}")
    for col in ("Scenario", a.name, b.name, "Error rate", "p99 ms"):
        t.add_column(col)
    for x, y in zip(c.runs_a, c.runs_b):
        t.add_row(
            x.scenario,
            f"[{STATUS_STYLE[x.status]}]{x.status}[/]",
            f"[{STATUS_STYLE[y.status]}]{y.status}[/]",
            f"{x.error_rate:.2%} to {y.error_rate:.2%}",
            f"{x.p99_ms:.0f} to {y.p99_ms:.0f}",
        )
    console.print(t)
    if c.new_findings:
        console.print(f"[bold]New weak spots in {b.name}:[/bold]")
        for f in c.new_findings:
            console.print(f"  [{SEV_STYLE[f.severity]}]{f.severity:<8}[/] {f.rule:<22} {f.target}: {f.message}")
    console.print(f"report: {_write(report_dir, f'compare_{a.name}_vs_{b.name}', comparison_markdown(c))}")


if __name__ == "__main__":
    app()
