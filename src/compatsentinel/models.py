"""Data model shared by capture, diff, report and MCP.

Everything that crosses a boundary (disk, terminal, AI assistant) is a pydantic
model declared here. Models are frozen and reject unknown fields, so a typo in
a snapshot or a suite file is an error, never silently ignored.

Naming: a *signal* is what one collector observed about one app run. An
``AppRun`` groups the signals for one app; a ``Snapshot`` groups the runs of a
whole suite together with the OS fingerprint they were taken on.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

SCHEMA_VERSION = 1
"""Bump when a change would make older readers misinterpret a snapshot."""


class StrictModel(BaseModel):
    """Base for every model: immutable and no unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Enumerations ------------------------------------------------------------


class Severity(StrEnum):
    INFO = "info"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Verdict(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class LaunchOutcome(StrEnum):
    """What happened when the app was started."""

    OK = "ok"
    """A window appeared (or the process was alive) and it survived the alive check."""
    EXITED = "exited"
    """The process ended before the alive check; see ``exit_code``."""
    TIMEOUT = "timeout"
    """No window matched within ``timeout_seconds``."""
    FAILED_TO_START = "failed_to_start"
    """The OS refused to start it (missing file, access denied, ...)."""


class CollectorStatus(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    """Not applicable here, e.g. needs elevation or a Windows-only API."""
    ERROR = "error"
    """The collector raised; the message is in ``CollectorResult.error``."""


# --- Suite defaults (shared with the suite loader) ----------------------------


class RunDefaults(StrictModel):
    """Timing knobs that apply to every app unless the app overrides them."""

    timeout_seconds: PositiveInt = 30
    alive_check_seconds: PositiveInt = 5
    repeats: PositiveInt = 3
    warmup_runs: int = Field(default=1, ge=0)


# --- Signals (one per collector) ---------------------------------------------


class LaunchSignal(StrictModel):
    outcome: LaunchOutcome
    exit_code: int | None = None
    startup_ms: float | None = None
    """Median of ``startup_samples_ms``; None when no sample completed."""
    startup_samples_ms: list[float] = []
    alive_after_check: bool | None = None
    pids: list[int] = []
    window_title: str | None = None
    error: str | None = None


class ModuleInfo(StrictModel):
    name: str
    path: str
    version: str | None = None
    is_system: bool = False
    """True when the file lives under the Windows directory."""


class EventLogEntry(StrictModel):
    log: str
    source: str
    event_id: int
    level: str
    timestamp: datetime
    message: str
    process_name: str | None = None


class WerReport(StrictModel):
    report_path: str
    event_type: str
    app_name: str
    timestamp: datetime
    app_version: str | None = None
    fault_module: str | None = None
    exception_code: str | None = None


class CollectorResult(StrictModel):
    """Bookkeeping for one collector on one app run."""

    name: str
    status: CollectorStatus
    duration_ms: float = 0.0
    error: str | None = None
    requires_elevation: bool = False


# --- Aggregates ----------------------------------------------------------------


class AppRun(StrictModel):
    """Everything observed for one app in one capture.

    A signal that is ``None`` means its collector did not produce data (skipped
    or failed); an empty list means it ran and found nothing. Diff rules rely on
    that distinction to avoid false positives.
    """

    app_id: str
    command: str
    args: list[str] = []
    tags: list[str] = []
    started_at: datetime
    finished_at: datetime
    launch: LaunchSignal | None = None
    modules: list[ModuleInfo] | None = None
    events: list[EventLogEntry] | None = None
    wer: list[WerReport] | None = None
    collectors: list[CollectorResult] = []


class Environment(StrictModel):
    """OS fingerprint. Context for findings, never a failure by itself."""

    os_name: str
    os_version: str
    """Raw version string such as ``10.0.26200``."""
    architecture: str
    python_version: str
    build: int | None = None
    ubr: int | None = None
    """Update Build Revision, the number after the build (26200.1234)."""
    display_version: str | None = None
    """Marketing version such as ``24H2``."""
    edition: str | None = None
    hotfixes: list[str] = []
    """Installed KB ids, sorted."""
    dotnet_runtimes: list[str] = []
    vcpp_runtimes: list[str] = []
    other_runtimes: list[str] = []


class Snapshot(StrictModel):
    schema_version: int = SCHEMA_VERSION
    label: str
    created_at: datetime
    tool_version: str
    environment: Environment
    defaults: RunDefaults = RunDefaults()
    apps: list[AppRun] = []

    def app(self, app_id: str) -> AppRun | None:
        """Return the run for ``app_id`` or None when the app was not in this suite."""
        return next((run for run in self.apps if run.app_id == app_id), None)


# --- Diff output ----------------------------------------------------------------


class Finding(StrictModel):
    """One detected change. Every finding must be explainable on its own."""

    rule_id: str
    severity: Severity
    message: str
    app_id: str | None = None
    """None for environment-level findings."""
    before: str | None = None
    after: str | None = None


# --- Diff configuration and result ---------------------------------------------


class DiffConfig(StrictModel):
    """Tunable thresholds for the diff rules. Defaults match the documented rules."""

    startup_regression_pct: float = Field(default=25.0, ge=0)
    """Median startup must be at least this much slower, in percent..."""
    startup_regression_ms: float = Field(default=300.0, ge=0)
    """...and at least this much slower in absolute terms. Both must hold."""


class AppDiff(StrictModel):
    """Outcome of comparing one app across two snapshots."""

    app_id: str
    compared: bool = True
    """False when the app is missing from one snapshot; then there are no findings."""
    note: str | None = None
    score: int = Field(ge=0, le=100)
    verdict: Verdict
    findings: list[Finding] = []
    unavailable_signals: list[str] = []
    """Signals a rule could not evaluate because a collector produced no data on one side."""
    before_outcome: LaunchOutcome | None = None
    after_outcome: LaunchOutcome | None = None
    before_startup_ms: float | None = None
    after_startup_ms: float | None = None


class DiffResult(StrictModel):
    """Everything ``diff`` computed. Pure data: no timestamps, no host details."""

    before_label: str
    after_label: str
    before_environment: Environment
    after_environment: Environment
    config: DiffConfig
    environment_findings: list[Finding] = []
    apps: list[AppDiff] = []
    verdict: Verdict

    def app(self, app_id: str) -> AppDiff | None:
        return next((item for item in self.apps if item.app_id == app_id), None)
