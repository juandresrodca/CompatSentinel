"""Render a diff result to the terminal with rich.

Presentation only: nothing here computes a verdict or a score. The same
:class:`DiffResult` feeds the HTML report and the MCP server.
"""

from __future__ import annotations

from collections import Counter

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from compatsentinel.models import AppDiff, DiffResult, Environment, Severity, Verdict

VERDICT_STYLE = {Verdict.PASS: "bold green", Verdict.WARN: "bold yellow", Verdict.FAIL: "bold red"}
SEVERITY_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.INFO: "dim",
}


def render(
    result: DiffResult, console: Console, *, show_info: bool = False, quiet: bool = False
) -> None:
    """Print the whole result. Info findings are summarised unless ``show_info``.

    When ``quiet`` is set, only the overall verdict line is printed. JSON and
    HTML writers are unaffected; they live outside this function.
    """
    if quiet:
        style = VERDICT_STYLE[result.verdict]
        console.print(f"Overall verdict: [{style}]{result.verdict.value.upper()}[/{style}]")
        return

    console.print(
        f"[bold]before[/bold]  {escape(result.before_label)}: {describe(result.before_environment)}"
    )
    console.print(
        f"[bold]after[/bold]   {escape(result.after_label)}: {describe(result.after_environment)}"
    )
    console.print()

    if result.environment_findings:
        console.print("[bold]Environment changes[/bold] (context, not scored)")
        for finding in result.environment_findings:
            console.print(f"  [dim]-[/dim] {escape(finding.message)}", highlight=False)
        console.print()

    console.print(_apps_table(result))

    for app in result.apps:
        if app.findings:
            _print_findings(app, console, show_info)

    style = VERDICT_STYLE[result.verdict]
    console.print(f"Overall verdict: [{style}]{result.verdict.value.upper()}[/{style}]")


def describe(env: Environment) -> str:
    parts = [env.os_name, env.os_version]
    if env.ubr is not None:
        parts.append(f"UBR {env.ubr}")
    if env.display_version:
        parts.append(f"({env.display_version})")
    parts.append(f"{len(env.hotfixes)} KBs")
    return escape(" ".join(parts))


def _apps_table(result: DiffResult) -> Table:
    table = Table(title="Apps", title_justify="left")
    table.add_column("app", style="bold")
    table.add_column("verdict")
    table.add_column("score", justify="right")
    table.add_column("launch (before -> after)")
    table.add_column("startup (before -> after)", justify="right")
    table.add_column("findings")
    table.add_column("notes", overflow="fold")
    for app in result.apps:
        style = VERDICT_STYLE[app.verdict]
        verdict = f"[{style}]{app.verdict.value}[/{style}]" if app.compared else "[dim]n/a[/dim]"
        notes = [note for note in (app.note,) if note]
        if app.unavailable_signals:
            notes.append("not compared: " + ", ".join(app.unavailable_signals))
        table.add_row(
            escape(app.app_id),
            verdict,
            str(app.score) if app.compared else "-",
            f"{_outcome(app.before_outcome)} -> {_outcome(app.after_outcome)}",
            f"{_ms(app.before_startup_ms)} -> {_ms(app.after_startup_ms)}",
            _finding_counts(app),
            escape("; ".join(notes) or "-"),
        )
    return table


def _print_findings(app: AppDiff, console: Console, show_info: bool) -> None:
    shown = [f for f in app.findings if show_info or f.severity is not Severity.INFO]
    hidden = [f for f in app.findings if f not in shown]

    if shown:
        table = Table(
            title=f"{escape(app.app_id)}: {len(app.findings)} finding(s)", title_justify="left"
        )
        table.add_column("rule")
        table.add_column("severity")
        table.add_column("what changed", overflow="fold")
        for finding in shown:
            style = SEVERITY_STYLE[finding.severity]
            table.add_row(
                finding.rule_id,
                f"[{style}]{finding.severity.value}[/{style}]",
                escape(finding.message),
            )
        console.print(table)

    if hidden:
        by_rule = Counter(f.rule_id for f in hidden)
        summary = ", ".join(f"{rule} x{count}" for rule, count in sorted(by_rule.items()))
        console.print(
            f"  [dim]{escape(app.app_id)}: {len(hidden)} info finding(s) hidden "
            f"({escape(summary)}); pass --show-info to list them[/dim]"
        )


def _finding_counts(app: AppDiff) -> str:
    counts = Counter(f.severity for f in app.findings)
    if not counts:
        return "[dim]none[/dim]"
    parts = []
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.INFO):
        if severity in counts:
            style = SEVERITY_STYLE[severity]
            parts.append(f"[{style}]{counts[severity]} {severity.value}[/{style}]")
    return ", ".join(parts)


def _outcome(value: object) -> str:
    return getattr(value, "value", None) or "-"


def _ms(value: float | None) -> str:
    return f"{value:.0f} ms" if value is not None else "-"
