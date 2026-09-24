"""Diff rules: each one is a small pure function from two app runs to findings.

A rule never touches the clock, the disk or the OS. It receives the ``before``
and ``after`` runs of the same app plus a :class:`RuleContext` with the
thresholds and whether the environment changed, and returns zero or more
:class:`Finding` objects, each carrying its own explanation.

Rules stay silent when a signal is missing (``None``) on either side: an
absent collector result is not evidence of a regression. The engine reports
those gaps separately as ``unavailable_signals``.

``RULES`` is the registry. It is a plain tuple: adding a rule means writing a
function and appending a :class:`Rule` entry, nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from compatsentinel.models import AppRun, DiffConfig, Environment, Finding, Severity

CRASH_PROVIDERS = frozenset({"application error", "windows error reporting"})
"""Event log providers whose error events are crash evidence, not generic errors."""

MAX_MESSAGE_PREVIEW = 160


@dataclass(frozen=True)
class RuleContext:
    config: DiffConfig
    environment_changed: bool
    """True when build, UBR or KB list differs; some rules soften their severity then."""


RuleCheck = Callable[[AppRun, AppRun, RuleContext], list[Finding]]


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    """The severity the rule normally raises; a rule may lower it in context."""
    summary: str
    """One line for docs and for the MCP ``explain_finding`` tool."""
    check: RuleCheck


# --- Rules ---------------------------------------------------------------------------


def launch_failed(before: AppRun, after: AppRun, ctx: RuleContext) -> list[Finding]:
    """The app launched and stayed up before, but not after."""
    b, a = before.launch, after.launch
    if b is None or a is None or b.outcome != "ok" or a.outcome == "ok":
        return []
    detail = a.error or (f"exit code {a.exit_code}" if a.exit_code is not None else None)
    message = f"launched fine before; after: {a.outcome.value}" + (f" ({detail})" if detail else "")
    return [_finding("LAUNCH_FAILED", Severity.CRITICAL, before, message, "ok", a.outcome.value)]


def crash_new(before: AppRun, after: AppRun, ctx: RuleContext) -> list[Finding]:
    """A WER report or Application Error event attributed to the app that did not exist before."""
    seen = set(_crash_evidence(before))
    fresh = [item for item in _crash_evidence(after) if item not in seen]
    baseline = "no crash evidence" if not seen else f"{len(seen)} crash(es) already present"
    return [
        _finding(
            "CRASH_NEW", Severity.CRITICAL, before, f"new crash evidence: {item}", baseline, item
        )
        for item in fresh
    ]


def exit_code_changed(before: AppRun, after: AppRun, ctx: RuleContext) -> list[Finding]:
    """The process ended with a different exit code."""
    b, a = before.launch, after.launch
    if b is None or a is None or b.exit_code is None or a.exit_code is None:
        return []
    if b.exit_code == a.exit_code:
        return []
    return [
        _finding(
            "EXIT_CODE_CHANGED",
            Severity.HIGH,
            before,
            f"exit code {format_exit_code(b.exit_code)} -> {format_exit_code(a.exit_code)}",
            format_exit_code(b.exit_code),
            format_exit_code(a.exit_code),
        )
    ]


def startup_regression(before: AppRun, after: AppRun, ctx: RuleContext) -> list[Finding]:
    """Median startup got slower by both the relative and the absolute threshold."""
    b, a = before.launch, after.launch
    if b is None or a is None or b.startup_ms is None or a.startup_ms is None or b.startup_ms <= 0:
        return []
    delta = a.startup_ms - b.startup_ms
    pct = delta / b.startup_ms * 100
    cfg = ctx.config
    if delta < cfg.startup_regression_ms or pct < cfg.startup_regression_pct:
        return []
    message = (
        f"median startup {b.startup_ms:.0f} ms -> {a.startup_ms:.0f} ms "
        f"(+{delta:.0f} ms, +{pct:.0f}%; thresholds {cfg.startup_regression_ms:.0f} ms "
        f"and {cfg.startup_regression_pct:.0f}%)"
    )
    return [
        _finding(
            "STARTUP_REGRESSION",
            Severity.MEDIUM,
            before,
            message,
            f"{b.startup_ms:.0f} ms",
            f"{a.startup_ms:.0f} ms",
        )
    ]


def module_missing(before: AppRun, after: AppRun, ctx: RuleContext) -> list[Finding]:
    """A DLL that was loaded before is no longer loaded."""
    if before.modules is None or after.modules is None:
        return []
    after_names = {m.name.lower() for m in after.modules}
    missing = sorted(
        (m for m in before.modules if m.name.lower() not in after_names),
        key=lambda m: m.name.lower(),
    )
    return [
        _finding(
            "MODULE_MISSING",
            Severity.MEDIUM,
            before,
            f"{m.name} was loaded before and is not loaded now",
            m.path,
            "not loaded",
        )
        for m in missing
    ]


def module_added(before: AppRun, after: AppRun, ctx: RuleContext) -> list[Finding]:
    """A DLL that was not loaded before is loaded now."""
    if before.modules is None or after.modules is None:
        return []
    before_names = {m.name.lower() for m in before.modules}
    added = sorted(
        (m for m in after.modules if m.name.lower() not in before_names),
        key=lambda m: m.name.lower(),
    )
    return [
        _finding(
            "MODULE_ADDED",
            Severity.INFO,
            before,
            f"{m.name} is now loaded (was not loaded before)",
            "not loaded",
            m.path,
        )
        for m in added
    ]


def module_version_changed(before: AppRun, after: AppRun, ctx: RuleContext) -> list[Finding]:
    """Same DLL, different file version.

    Info by default. A *system* DLL changing version is medium, unless the OS
    build or KB list changed too: then updated system DLLs are the expected
    consequence of the update, not a signal on their own.
    """
    if before.modules is None or after.modules is None:
        return []
    after_by_name = {m.name.lower(): m for m in after.modules}
    findings: list[Finding] = []
    for old in sorted(before.modules, key=lambda m: m.name.lower()):
        new = after_by_name.get(old.name.lower())
        if new is None or old.version is None or new.version is None or old.version == new.version:
            continue
        suspicious = old.is_system and not ctx.environment_changed
        severity = Severity.MEDIUM if suspicious else Severity.INFO
        message = f"{old.name}: {old.version} -> {new.version}"
        if suspicious:
            message += " (system DLL changed without an OS update)"
        elif old.is_system:
            message += " (system DLL, expected after the OS update)"
        findings.append(
            _finding("MODULE_VERSION_CHANGED", severity, before, message, old.version, new.version)
        )
    return findings


def eventlog_new_errors(before: AppRun, after: AppRun, ctx: RuleContext) -> list[Finding]:
    """New error events tied to the app, excluding crash events (those are CRASH_NEW)."""
    if before.events is None or after.events is None:
        return []
    seen = {_event_key(e) for e in before.events}
    fresh = [
        e
        for e in after.events
        if _event_key(e) not in seen and e.source.lower() not in CRASH_PROVIDERS
    ]
    return [
        _finding(
            "EVENTLOG_NEW_ERRORS",
            Severity.HIGH,
            before,
            f"new {e.level} event from {e.source} (id {e.event_id}): {_preview(e.message)}",
            "no such event",
            f"{e.source} {e.event_id}: {_preview(e.message)}",
        )
        for e in fresh
    ]


def window_title_changed(before: AppRun, after: AppRun, ctx: RuleContext) -> list[Finding]:
    """The main window title changed, e.g. a compatibility-mode or safe-mode suffix appeared.

    Info severity. Both launches must have recorded a title; a missing title on
    either side is not a change.
    """
    return []  # TODO(juan): implement; see tests/test_rules.py


RULES: tuple[Rule, ...] = (
    Rule(
        "LAUNCH_FAILED",
        Severity.CRITICAL,
        "App launched before, fails or times out after.",
        launch_failed,
    ),
    Rule(
        "CRASH_NEW",
        Severity.CRITICAL,
        "New WER report or Application Error event for the app.",
        crash_new,
    ),
    Rule(
        "EXIT_CODE_CHANGED",
        Severity.HIGH,
        "Exit code differs between the two runs.",
        exit_code_changed,
    ),
    Rule(
        "STARTUP_REGRESSION",
        Severity.MEDIUM,
        "Median startup slower by at least 25% and 300 ms (configurable).",
        startup_regression,
    ),
    Rule(
        "MODULE_MISSING",
        Severity.MEDIUM,
        "A DLL loaded before is no longer loaded.",
        module_missing,
    ),
    Rule(
        "MODULE_ADDED",
        Severity.INFO,
        "A DLL not loaded before is now loaded.",
        module_added,
    ),
    Rule(
        "MODULE_VERSION_CHANGED",
        Severity.INFO,
        "Same DLL, different file version; medium for a system DLL without an OS update.",
        module_version_changed,
    ),
    Rule(
        "EVENTLOG_NEW_ERRORS",
        Severity.HIGH,
        "New error events tied to the app process.",
        eventlog_new_errors,
    ),
    Rule(
        "WINDOW_TITLE_CHANGED",
        Severity.INFO,
        "The main window title changed.",
        window_title_changed,
    ),
)

ENV_RULE = Rule(
    "ENV_CHANGED",
    Severity.INFO,
    "OS build, UBR, KB list or runtimes differ (context, never a failure).",
    lambda b, a, c: [],
)

RULES_BY_ID: dict[str, Rule] = {rule.id: rule for rule in (*RULES, ENV_RULE)}


# --- Environment rule (snapshot level, not per app) --------------------------------------


def environment_findings(before: Environment, after: Environment) -> list[Finding]:
    """One info finding per environment field that differs."""
    findings: list[Finding] = []
    scalar_fields = ("os_name", "os_version", "build", "ubr", "display_version", "edition")
    for name in scalar_fields:
        old, new = getattr(before, name), getattr(after, name)
        if old != new:
            findings.append(_env_finding(f"{name} {old} -> {new}", str(old), str(new)))
    for name in ("hotfixes", "dotnet_runtimes", "vcpp_runtimes", "other_runtimes"):
        added, removed = list_delta(getattr(before, name), getattr(after, name))
        if added or removed:
            parts = []
            if added:
                parts.append("added " + ", ".join(added))
            if removed:
                parts.append("removed " + ", ".join(removed))
            findings.append(
                _env_finding(
                    f"{name}: " + "; ".join(parts),
                    ", ".join(getattr(before, name)) or "-",
                    ", ".join(getattr(after, name)) or "-",
                )
            )
    return findings


def list_delta(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:
    """``(added, removed)`` between two lists, order-insensitive, each sorted."""
    old, new = set(before), set(after)
    return sorted(new - old), sorted(old - new)


# --- Helpers -------------------------------------------------------------------------------


def _finding(
    rule_id: str,
    severity: Severity,
    run: AppRun,
    message: str,
    before: str | None,
    after: str | None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        app_id=run.app_id,
        message=message,
        before=before,
        after=after,
    )


def _env_finding(message: str, before: str, after: str) -> Finding:
    return Finding(
        rule_id=ENV_RULE.id, severity=Severity.INFO, message=message, before=before, after=after
    )


def _crash_evidence(run: AppRun) -> list[str]:
    evidence: list[str] = []
    for report in run.wer or []:
        parts = [f"WER {report.event_type or 'report'} for {report.app_name}"]
        if report.fault_module:
            parts.append(f"fault module {report.fault_module}")
        if report.exception_code:
            parts.append(f"exception {report.exception_code}")
        evidence.append(", ".join(parts))
    for event in run.events or []:
        if event.source.lower() in CRASH_PROVIDERS:
            evidence.append(f"event {event.source} {event.event_id}: {_preview(event.message)}")
    return evidence


def _event_key(event: object) -> tuple[str, int, str]:
    source = str(getattr(event, "source", "")).lower()
    return (source, int(getattr(event, "event_id", 0)), str(getattr(event, "message", "")))


def format_exit_code(code: int) -> str:
    """Decimal, plus the hex NTSTATUS form Windows people recognise for odd values.

    ``-1073741819`` means nothing at a glance; ``0xC0000005`` is an access
    violation to anyone who has read a crash dialog.
    """
    if 0 <= code <= 0xFFFF:
        return str(code)
    return f"{code} (0x{code & 0xFFFFFFFF:08X})"


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE_PREVIEW else text[: MAX_MESSAGE_PREVIEW - 1] + "…"
