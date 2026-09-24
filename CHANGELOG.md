# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `MODULE_ADDED` diff findings for DLLs first observed after an update.
- Command-line overrides for capture timeout, repeats, alive checks, and warmup runs.

## [0.1.0] - 2026-09-19

Initial release.

### Added

- `compatsentinel capture`: launches each app in a suite with process
  attribution, window detection and startup timing, then records loaded
  modules, Application log errors and WER reports. Windows 10/11 only.
- Environment fingerprint: OS build, UBR, display version, edition,
  installed KBs, and .NET and VC++ runtimes.
- `compatsentinel diff`: pure, deterministic comparison of two snapshots
  with eight rules (LAUNCH_FAILED, CRASH_NEW, EXIT_CODE_CHANGED,
  STARTUP_REGRESSION, MODULE_MISSING, MODULE_VERSION_CHANGED,
  EVENTLOG_NEW_ERRORS, ENV_CHANGED), a per-app risk score, PASS/WARN/FAIL
  verdicts, `--json` export and `--fail-on` exit codes for CI. Runs on any
  OS.
- `compatsentinel report` and `diff --html`: single-file, offline HTML
  report with light and dark mode and the full diff result embedded as
  JSON.
- `compatsentinel mcp`: read-only MCP server (`list_snapshots`,
  `get_environment`, `get_app_run`, `diff`, `explain_finding`) plus a
  `snapshot://{label}` resource, for use with Claude Desktop or any MCP
  client. Verified with the MCP Inspector.
- `compatsentinel validate` and `compatsentinel doctor`.
- Demo snapshots under `examples/snapshots` (a 23H2 to 24H2 upgrade story)
  so `diff`, `report` and `mcp` can be tried without Windows.
- Versioned JSON snapshot store, a pydantic v2 data model with a schema
  version, and a suite loader (`apps.yaml`) with readable validation errors.
- `python -m compatsentinel` as an alternative entry point.
- CI on Ubuntu (lint, types, tests) and Windows (real capture, including a
  full `compatsentinel capture` run against notepad on `windows-latest`).
