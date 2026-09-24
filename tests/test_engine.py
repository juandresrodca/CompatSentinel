"""Engine: app matching, signal gaps, determinism, and the shipped demo snapshots."""

from __future__ import annotations

from pathlib import Path

from compatsentinel.diff import diff_snapshots
from compatsentinel.models import DiffConfig, LaunchOutcome, Verdict
from compatsentinel.store import SnapshotStore
from tests.factories import make_environment, make_launch, make_run, make_snapshot, make_wer

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "snapshots"


def test_identical_snapshots_pass_with_no_findings() -> None:
    result = diff_snapshots(make_snapshot("a"), make_snapshot("b"))
    assert result.verdict is Verdict.PASS
    assert result.environment_findings == []
    assert [(app.app_id, app.score, app.findings) for app in result.apps] == [("notepad", 0, [])]
    assert result.before_label == "a" and result.after_label == "b"


def test_other_runtime_changes_are_reported() -> None:
    before = make_snapshot(environment=make_environment(other_runtimes=["Java 17.0.12"]))
    after = make_snapshot(environment=make_environment(other_runtimes=["Java 21.0.8"]))

    assert [finding.message for finding in diff_snapshots(before, after).environment_findings] == [
        "other_runtimes: added Java 21.0.8; removed Java 17.0.12"
    ]


def test_apps_missing_on_one_side_are_reported_not_scored() -> None:
    before = make_snapshot(apps=[make_run("a"), make_run("b")])
    after = make_snapshot(apps=[make_run("b"), make_run("c")])
    result = diff_snapshots(before, after)
    assert [app.app_id for app in result.apps] == ["a", "b", "c"]  # before order, then new ones
    a, c = result.app("a"), result.app("c")
    assert a is not None and not a.compared and a.note == "not present in the after snapshot"
    assert c is not None and not c.compared and c.note == "not present in the before snapshot"
    assert a.verdict is Verdict.PASS and a.findings == []


def test_unavailable_signals_are_listed_per_app() -> None:
    before = make_snapshot(apps=[make_run("a", no_modules=True)])
    after = make_snapshot(apps=[make_run("a", no_wer=True)])
    app = diff_snapshots(before, after).app("a")
    assert app is not None
    assert app.unavailable_signals == ["modules", "wer"]
    assert app.findings == []


def test_app_ids_filter_and_summary_fields() -> None:
    before = make_snapshot(apps=[make_run("a"), make_run("b")])
    after = make_snapshot(
        apps=[
            make_run(
                "a", launch=make_launch(outcome=LaunchOutcome.EXITED, startup_ms=900.0, exit_code=2)
            ),
            make_run("b"),
        ]
    )
    result = diff_snapshots(before, after, app_ids=["a"])
    assert [app.app_id for app in result.apps] == ["a"]
    app = result.apps[0]
    assert (app.before_outcome, app.after_outcome) == (LaunchOutcome.OK, LaunchOutcome.EXITED)
    assert (app.before_startup_ms, app.after_startup_ms) == (400.0, 900.0)
    assert {f.rule_id for f in app.findings} == {
        "LAUNCH_FAILED",
        "EXIT_CODE_CHANGED",
        "STARTUP_REGRESSION",
    }
    assert app.score == 100 and app.verdict is Verdict.FAIL
    assert result.verdict is Verdict.FAIL


def test_environment_change_softens_system_dll_findings() -> None:
    before = make_snapshot(apps=[make_run("a")])
    bumped = [
        m.model_copy(update={"version": "10.0.26100.9999"}) if m.is_system else m
        for m in make_run().modules or []
    ]
    after_same_env = make_snapshot(apps=[make_run("a", modules=bumped)])
    after_new_env = make_snapshot(
        apps=[make_run("a", modules=bumped)], environment=make_environment(ubr=9999)
    )

    same = diff_snapshots(before, after_same_env).app("a")
    new = diff_snapshots(before, after_new_env).app("a")
    assert same is not None and same.verdict is Verdict.WARN
    assert new is not None and new.verdict is Verdict.PASS
    assert diff_snapshots(before, after_new_env).environment_findings


def test_config_is_applied_and_echoed() -> None:
    before = make_snapshot(apps=[make_run("a", launch=make_launch(startup_ms=1000.0))])
    after = make_snapshot(apps=[make_run("a", launch=make_launch(startup_ms=1100.0))])
    loose = diff_snapshots(before, after)
    strict = diff_snapshots(
        before, after, DiffConfig(startup_regression_pct=5, startup_regression_ms=50)
    )
    assert loose.apps[0].findings == []
    assert [f.rule_id for f in strict.apps[0].findings] == ["STARTUP_REGRESSION"]
    assert strict.config.startup_regression_pct == 5


def test_diff_is_deterministic_and_serialisable() -> None:
    before = make_snapshot(apps=[make_run("a")])
    after = make_snapshot(apps=[make_run("a", wer=[make_wer()])])
    first, second = diff_snapshots(before, after), diff_snapshots(before, after)
    assert first == second
    assert first.model_validate_json(first.model_dump_json()) == first


# --- The shipped demo: examples/snapshots must tell the documented story --------------


def test_example_snapshots_reproduce_the_documented_regressions() -> None:
    store = SnapshotStore(EXAMPLES)
    result = diff_snapshots(store.load(EXAMPLES / "before"), store.load(EXAMPLES / "after"))

    assert result.verdict is Verdict.FAIL
    assert {f.message.split(" ")[0] for f in result.environment_findings} >= {
        "build",
        "ubr",
        "display_version",
        "hotfixes:",
    }

    verdicts = {app.app_id: app.verdict for app in result.apps}
    assert verdicts == {
        "notepad": Verdict.WARN,
        "calculator": Verdict.WARN,
        "contoso-ledger": Verdict.FAIL,
        "wordpad-legacy": Verdict.FAIL,
    }

    def rule_ids(app_id: str) -> set[str]:
        app = result.app(app_id)
        assert app is not None
        return {f.rule_id for f in app.findings}

    assert rule_ids("notepad") == {"STARTUP_REGRESSION", "MODULE_VERSION_CHANGED"}
    assert rule_ids("calculator") == {"MODULE_MISSING", "MODULE_VERSION_CHANGED"}
    assert rule_ids("contoso-ledger") == {
        "LAUNCH_FAILED",
        "CRASH_NEW",
        "EXIT_CODE_CHANGED",
        "EVENTLOG_NEW_ERRORS",
    }
    assert rule_ids("wordpad-legacy") == {"LAUNCH_FAILED"}

    ledger = result.app("contoso-ledger")
    assert ledger is not None and ledger.unavailable_signals == ["modules"]
