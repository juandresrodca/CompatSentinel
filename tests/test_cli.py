"""CLI surface tests: these run on every OS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from compatsentinel import __version__, cli
from compatsentinel.cli import app
from compatsentinel.models import Snapshot
from compatsentinel.suite import Suite
from tests.conftest import SnapshotFactory


def test_help_lists_doctor(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output


def test_no_args_shows_help(runner: CliRunner) -> None:
    result = runner.invoke(app, [])
    # Typer exits 0 for --help but 2 when help is shown because no args were given.
    assert result.exit_code in (0, 2)
    assert "Usage" in result.output


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_doctor_runs_anywhere(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Python" in result.output
    assert "pywin32" in result.output


def test_validate_example_suite(runner: CliRunner) -> None:
    result = runner.invoke(app, ["validate", "examples/apps.yaml"])
    assert result.exit_code == 0, result.output
    assert "notepad" in result.output
    assert "Suite is valid" in result.output


def test_validate_reports_errors_and_exits_1(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("apps:\n  - id: notepad\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "apps.0.command" in result.output


def test_capture_cli_overrides_defaults_but_not_per_app_settings(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_snapshot: SnapshotFactory,
) -> None:
    suite_path = tmp_path / "apps.yaml"
    suite_path.write_text(
        "defaults:\n"
        "  timeout_seconds: 30\n"
        "  repeats: 3\n"
        "apps:\n"
        "  - id: app\n"
        "    command: app.exe\n"
        "    repeats: 7\n",
        encoding="utf-8",
    )
    captured: list[Suite] = []

    def fake_capture(loaded: Suite, label: str, **_: object) -> Snapshot:
        captured.append(loaded)
        return make_snapshot(label=label, defaults=loaded.defaults)

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.runner, "capture", fake_capture)
    result = runner.invoke(
        app,
        [
            "capture",
            "--suite",
            str(suite_path),
            "--label",
            "override",
            "--store",
            str(tmp_path / "snapshots"),
            "--timeout-seconds",
            "12",
            "--repeats",
            "2",
            "--alive-check-seconds",
            "4",
            "--warmup-runs",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured[0].defaults.model_dump() == {
        "timeout_seconds": 12,
        "alive_check_seconds": 4,
        "repeats": 2,
        "warmup_runs": 0,
    }
    assert captured[0].apps[0].effective(captured[0].defaults).repeats == 7


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--timeout-seconds", "0"),
        ("--repeats", "0"),
        ("--alive-check-seconds", "0"),
        ("--warmup-runs", "-1"),
    ],
)
def test_capture_cli_overrides_reject_invalid_bounds(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    value: str,
) -> None:
    monkeypatch.setattr(cli.sys, "platform", "win32")
    result = runner.invoke(
        app,
        [
            "capture",
            "--suite",
            "examples/apps.yaml",
            "--label",
            "invalid",
            "--store",
            str(tmp_path),
            option,
            value,
        ],
    )
    assert result.exit_code == 2


EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "snapshots"


def test_diff_examples_fails_by_default(runner: CliRunner) -> None:
    result = runner.invoke(app, ["diff", str(EXAMPLES / "before"), str(EXAMPLES / "after")])
    assert result.exit_code == 1, result.output
    assert "Overall verdict: FAIL" in result.output
    assert "contoso-ledger" in result.output
    assert "LAUNCH_FAILED" in result.output
    assert "Environment changes" in result.output


def test_diff_fail_on_never_and_json_export(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "diff.json"
    result = runner.invoke(
        app,
        [
            "diff",
            str(EXAMPLES / "before"),
            str(EXAMPLES / "after"),
            "--fail-on",
            "never",
            "--json",
            str(out),
            "--app",
            "notepad",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "warn"
    assert [item["app_id"] for item in data["apps"]] == ["notepad"]


def test_diff_fail_on_warn(runner: CliRunner) -> None:
    args = ["diff", str(EXAMPLES / "before"), str(EXAMPLES / "after"), "--app", "notepad"]
    assert runner.invoke(app, args).exit_code == 0  # WARN does not fail by default
    assert runner.invoke(app, [*args, "--fail-on", "warn"]).exit_code == 1


def test_diff_thresholds_are_tunable(runner: CliRunner) -> None:
    args = ["diff", str(EXAMPLES / "before"), str(EXAMPLES / "after"), "--app", "notepad"]
    relaxed = runner.invoke(app, [*args, "--startup-pct", "200"])
    assert relaxed.exit_code == 0
    assert "STARTUP_REGRESSION" not in relaxed.output


def test_diff_with_labels_under_store(runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["diff", "before", "after", "--store", str(EXAMPLES), "--fail-on", "never"]
    )
    assert result.exit_code == 0, result.output


def test_diff_missing_snapshot_is_a_clean_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["diff", "nope", "after", "--store", str(tmp_path)])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_diff_hides_info_findings_unless_asked(runner: CliRunner) -> None:
    args = ["diff", str(EXAMPLES / "before"), str(EXAMPLES / "after"), "--app", "notepad"]
    quiet = runner.invoke(app, args)
    assert "info finding(s) hidden" in quiet.output
    assert "MODULE_VERSION_CHANGED" not in quiet.output.split("finding(s) hidden")[0]
    verbose = runner.invoke(app, [*args, "--show-info"])
    assert "hidden" not in verbose.output
    assert verbose.output.count("MODULE_VERSION_CHANGED") >= 14


def test_report_writes_html(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    result = runner.invoke(
        app, ["report", str(EXAMPLES / "before"), str(EXAMPLES / "after"), "--html", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "FAIL" in result.output
    text = out.read_text(encoding="utf-8")
    assert text.lower().startswith("<!doctype html>") and "contoso-ledger" in text


def test_diff_can_also_write_html(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "r.html"
    args = ["diff", str(EXAMPLES / "before"), str(EXAMPLES / "after"), "--html", str(out)]
    assert runner.invoke(app, [*args, "--fail-on", "never"]).exit_code == 0
    assert out.is_file()


# --- Juan's Phase 4 task (b) --------------------------------------------------------------
# Spec: open_in_browser(path) -> bool   (cli.py)
#   * Call webbrowser.open(path.resolve().as_uri()) and return its result.
#   * Catch any Exception and return False; never raise.
# Test with monkeypatch so no browser actually opens. Remove the skip when done.


@pytest.mark.skip(reason="TODO(juan): implement open_in_browser")
def test_report_open_flag_uses_webbrowser(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)
    out = tmp_path / "report.html"
    args = [
        "report",
        str(EXAMPLES / "before"),
        str(EXAMPLES / "after"),
        "--html",
        str(out),
        "--open",
    ]
    assert runner.invoke(app, args).exit_code == 0
    assert opened == [out.resolve().as_uri()]
