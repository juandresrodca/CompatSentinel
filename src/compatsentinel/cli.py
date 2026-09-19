"""Command line entry point (``compatsentinel``).

Commands are added phase by phase. Keep this module thin: parse arguments,
call into the library, render the result. No business logic lives here.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from compatsentinel import __version__, doctor, mcp_server, models, runner, store, suite
from compatsentinel.diff import diff_snapshots, scoring
from compatsentinel.report import html, terminal

app = typer.Typer(
    name="compatsentinel",
    help="Catch Windows app compatibility regressions before your users do.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"compatsentinel {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Capture behavioural fingerprints of Windows apps and diff them across updates."""
    # Library code logs degraded signals (e.g. a registry key it could not read);
    # surface those as warnings without cluttering normal output.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
    )


@app.command("doctor")
def doctor_command() -> None:
    """Check Python, OS and optional Windows dependencies."""
    report = doctor.collect()

    table = Table(title="CompatSentinel doctor", show_header=False)
    table.add_column("Check", style="bold")
    table.add_column("Value")
    table.add_row("Python", report.python_version)
    table.add_row("Interpreter", report.python_executable)
    # platform.release() reports "10" on Windows 11, so show the build instead.
    table.add_row("OS", f"{report.os_name} {report.os_version}")
    table.add_row("Machine", report.machine)
    table.add_row("pywin32", _yes_no(report.pywin32_available))
    table.add_row("Capture supported", _yes_no(report.capture_supported))
    if report.wer_enabled is not None:
        table.add_row("Windows Error Reporting", _yes_no(report.wer_enabled))
    console.print(table)

    if report.wer_enabled is False:
        console.print(
            "[yellow]WER is disabled on this host: crash reports and Application Error "
            "events will not be produced, so CRASH_NEW cannot fire.[/yellow]"
        )

    if not report.capture_supported:
        console.print(
            "[yellow]Capture needs Windows 10/11. diff, report and mcp work on any OS.[/yellow]"
        )


@app.command()
def validate(
    suite_path: Annotated[
        Path, typer.Argument(help="Path to the suite file (apps.yaml).", metavar="SUITE")
    ],
) -> None:
    """Validate a suite file and list the apps it would run."""
    try:
        loaded = suite.load_suite(suite_path)
    except suite.SuiteError as exc:
        # markup=False: validation messages may contain [brackets] rich would eat.
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(code=1) from None

    table = Table(title=f"{suite_path}: {len(loaded.apps)} app(s)")
    table.add_column("id", style="bold")
    table.add_column("command", overflow="fold")
    table.add_column("window regex")
    table.add_column("timeout", justify="right")
    table.add_column("repeats", justify="right")
    table.add_column("tags")
    for spec in loaded.apps:
        settings = spec.effective(loaded.defaults)
        table.add_row(
            spec.id,
            " ".join([spec.command, *spec.args]),
            spec.window_title_regex or "-",
            f"{settings.timeout_seconds}s",
            str(settings.repeats),
            ", ".join(spec.tags) or "-",
        )
    console.print(table)
    console.print("[green]Suite is valid.[/green]")


@app.command()
def capture(
    suite_path: Annotated[
        Path, typer.Option("--suite", "-s", help="Suite file (apps.yaml).", metavar="SUITE")
    ],
    label: Annotated[
        str, typer.Option("--label", "-l", help="Name for this snapshot, e.g. 'before'.")
    ],
    store_dir: Annotated[
        Path, typer.Option("--store", help="Directory that holds snapshots.")
    ] = Path("snapshots"),
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Run only this app id (repeatable).")
    ] = None,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace an existing snapshot with this label.")
    ] = False,
) -> None:
    """Launch every app in the suite and record its behavioural fingerprint (Windows only)."""
    if sys.platform != "win32":
        _fail("capture needs Windows 10 or 11. diff, report and mcp run anywhere.")

    snapshots = store.SnapshotStore(store_dir)
    try:
        store.validate_label(label)
        if snapshots.exists(label) and not overwrite:
            _fail(
                f"snapshot {label!r} already exists in {store_dir}; use --overwrite or a new label"
            )
        loaded = suite.load_suite(suite_path)
    except (store.StoreError, suite.SuiteError) as exc:
        _fail(str(exc))

    if only:
        unknown = sorted(set(only) - {spec.id for spec in loaded.apps})
        if unknown:
            _fail(f"unknown app id(s): {', '.join(unknown)}")

    snapshot = runner.capture(
        loaded, label, only=only, progress=lambda line: console.print(f"[dim]>[/dim] {line}")
    )
    path = snapshots.save(snapshot, overwrite=overwrite)
    _print_capture_summary(snapshot)
    console.print(f"[green]Saved[/green] {path}")


class FailOn(StrEnum):
    """Which verdict makes ``diff`` exit non-zero, for use in scripts and CI."""

    NEVER = "never"
    WARN = "warn"
    FAIL = "fail"


@app.command()
def diff(
    before: Annotated[
        str, typer.Argument(help="Label under --store, a snapshot folder, or a snapshot.json.")
    ],
    after: Annotated[str, typer.Argument(help="Same forms as BEFORE.")],
    store_dir: Annotated[
        Path, typer.Option("--store", help="Directory that holds snapshots.")
    ] = Path("snapshots"),
    app_ids: Annotated[
        list[str] | None, typer.Option("--app", help="Compare only this app id (repeatable).")
    ] = None,
    startup_pct: Annotated[
        float, typer.Option(help="Startup regression threshold, percent.", min=0)
    ] = 25.0,
    startup_ms: Annotated[
        float, typer.Option(help="Startup regression threshold, milliseconds.", min=0)
    ] = 300.0,
    json_path: Annotated[
        Path | None, typer.Option("--json", help="Also write the full result as JSON here.")
    ] = None,
    fail_on: Annotated[
        FailOn, typer.Option(help="Exit with status 1 at this verdict or worse.")
    ] = FailOn.FAIL,
    show_info: Annotated[
        bool,
        typer.Option("--show-info", help="List info-level findings instead of summarising them."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Print only the overall verdict line (JSON/HTML writers still run).",
        ),
    ] = False,
    html_path: Annotated[
        Path | None, typer.Option("--html", help="Also write a single-file HTML report here.")
    ] = None,
) -> None:
    """Compare two snapshots and print the findings with a risk score per app."""
    result = _run_diff(before, after, store_dir, app_ids, startup_pct, startup_ms)

    terminal.render(result, console, show_info=show_info, quiet=quiet)
    if json_path is not None:
        json_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Wrote[/green] {json_path}")
    if html_path is not None:
        html.write_report(result, html_path, generated_at=datetime.now(UTC))
        console.print(f"[green]Wrote[/green] {html_path}")

    threshold = {
        FailOn.NEVER: None,
        FailOn.WARN: models.Verdict.WARN,
        FailOn.FAIL: models.Verdict.FAIL,
    }[fail_on]
    if threshold is not None and scoring.VERDICT_ORDER.index(
        result.verdict
    ) >= scoring.VERDICT_ORDER.index(threshold):
        raise typer.Exit(code=1)


@app.command()
def report(
    before: Annotated[
        str, typer.Argument(help="Label under --store, a snapshot folder, or a snapshot.json.")
    ],
    after: Annotated[str, typer.Argument(help="Same forms as BEFORE.")],
    html_path: Annotated[
        Path, typer.Option("--html", "-o", help="Where to write the single-file HTML report.")
    ] = Path("report.html"),
    store_dir: Annotated[
        Path, typer.Option("--store", help="Directory that holds snapshots.")
    ] = Path("snapshots"),
    app_ids: Annotated[
        list[str] | None, typer.Option("--app", help="Compare only this app id (repeatable).")
    ] = None,
    startup_pct: Annotated[
        float, typer.Option(help="Startup regression threshold, percent.", min=0)
    ] = 25.0,
    startup_ms: Annotated[
        float, typer.Option(help="Startup regression threshold, milliseconds.", min=0)
    ] = 300.0,
    open_after: Annotated[
        bool, typer.Option("--open", help="Open the report in the default browser afterwards.")
    ] = False,
) -> None:
    """Write a self-contained HTML report comparing two snapshots."""
    result = _run_diff(before, after, store_dir, app_ids, startup_pct, startup_ms)
    path = html.write_report(result, html_path, generated_at=datetime.now(UTC))
    style = terminal.VERDICT_STYLE[result.verdict]
    console.print(
        f"[green]Wrote[/green] {path}  (verdict: [{style}]{result.verdict.value.upper()}[/{style}])"
    )
    if open_after:
        open_in_browser(path)


def open_in_browser(path: Path) -> bool:
    """Open ``path`` with the default browser; return whether a browser was launched.

    Must never raise: on a headless host there is no browser, and the report
    was still written. Use ``Path.resolve()`` and ``as_uri()`` so the file is
    opened by URL, which works on Windows and Unix alike.
    """
    return False  # TODO(juan): implement; see tests/test_cli.py


def _run_diff(
    before: str,
    after: str,
    store_dir: Path,
    app_ids: list[str] | None,
    startup_pct: float,
    startup_ms: float,
) -> models.DiffResult:
    """Load both snapshots and diff them, turning store errors into a clean exit."""
    snapshots = store.SnapshotStore(store_dir)
    try:
        before_snapshot = snapshots.load(before)
        after_snapshot = snapshots.load(after)
    except store.StoreError as exc:
        _fail(str(exc))
    config = models.DiffConfig(startup_regression_pct=startup_pct, startup_regression_ms=startup_ms)
    return diff_snapshots(before_snapshot, after_snapshot, config, app_ids)


@app.command("mcp")
def mcp_command(
    store_dir: Annotated[
        Path, typer.Option("--store", help="Directory that holds snapshots.")
    ] = Path("snapshots"),
) -> None:
    """Run the read-only MCP server over stored snapshots (stdio transport).

    Exposes list_snapshots, get_environment, get_app_run, diff and
    explain_finding to an MCP client such as Claude Desktop or the MCP
    Inspector. No tool can launch, close or otherwise touch a process; see
    src/compatsentinel/mcp_server.py.
    """
    mcp_server.run(store_dir)


def _print_capture_summary(snapshot: models.Snapshot) -> None:
    env = snapshot.environment
    console.print(
        f"[bold]{env.os_name}[/bold] {env.os_version}"
        + (f" UBR {env.ubr}" if env.ubr is not None else "")
        + (f" ({env.display_version})" if env.display_version else "")
        + f", {len(env.hotfixes)} hotfixes"
    )
    table = Table(title=f"snapshot '{snapshot.label}': {len(snapshot.apps)} app(s)")
    table.add_column("app", style="bold")
    table.add_column("outcome")
    table.add_column("startup", justify="right")
    table.add_column("modules", justify="right")
    table.add_column("events", justify="right")
    table.add_column("wer", justify="right")
    table.add_column("collector notes")
    for run in snapshot.apps:
        launch = run.launch
        outcome = launch.outcome.value if launch else "-"
        style = "green" if outcome == "ok" else "red"
        startup = f"{launch.startup_ms:.0f} ms" if launch and launch.startup_ms else "-"
        notes = "; ".join(
            f"{r.name}: {r.status.value}" + (f" ({r.error})" if r.error else "")
            for r in run.collectors
            if r.status is not models.CollectorStatus.OK
        )
        table.add_row(
            run.app_id,
            f"[{style}]{outcome}[/{style}]",
            startup,
            str(len(run.modules)) if run.modules is not None else "-",
            str(len(run.events)) if run.events is not None else "-",
            str(len(run.wer)) if run.wer is not None else "-",
            notes or "-",
        )
    console.print(table)


def _fail(message: str) -> NoReturn:
    console.print(message, style="red", markup=False)
    raise typer.Exit(code=1)


def _yes_no(value: bool) -> str:
    return "[green]yes[/green]" if value else "[red]no[/red]"
