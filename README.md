# CompatSentinel

[![CI](https://github.com/juandresrodca/CompatSentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/juandresrodca/CompatSentinel/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

> Catch Windows app compatibility regressions before your users do.

CompatSentinel captures the **behavioural fingerprint** of a set of Windows
applications (launch success, startup time, loaded DLLs, event log errors,
crash reports) and diffs two captures to flag regressions with a risk score.
Run it before and after Patch Tuesday, across two OS builds, or between two
machine configurations.

<!--
DEMO GIF: record a ~20s terminal capture (asciinema or ScreenToGif at
~1000x600, light theme, 14pt+ font) showing, in order:
  1. `compatsentinel capture --suite apps.yaml --label before` — a couple of
     lines of progress output is enough, no need to wait for it to finish.
  2. Simulate installing an update, then
     `compatsentinel capture --suite apps.yaml --label after`.
  3. `compatsentinel diff before after` — let the Apps table and at least one
     findings table render fully.
  4. `compatsentinel report before after --html report.html --open` — end on
     the HTML report opening in a browser, scrolled to the contoso-ledger
     card.
Save as docs/demo.gif (keep it under ~5 MB) and replace this comment with:
  ![CompatSentinel: capture, diff and report](docs/demo.gif)
-->

```text
compatsentinel capture --suite apps.yaml --label before
# install updates, reboot, or move to another build
compatsentinel capture --suite apps.yaml --label after
compatsentinel diff before after --html report.html
compatsentinel mcp            # expose snapshots to an AI assistant, read-only
```

**Status:** v0.1.0. `capture`, `diff`, `report`, `mcp`, `doctor` and
`validate` all exist today and are covered by CI on both Ubuntu and Windows.
Follow the [changelog](CHANGELOG.md) and the
[roadmap](#roadmap).

## Try it in 30 seconds, no Windows required

The repository ships two recorded snapshots that tell a Patch Tuesday story:
a Windows 11 23H2 machine upgraded to 24H2. Notepad got slower, Calculator
lost a DLL, a line-of-business app started crashing, and WordPad is gone.

```bash
pipx install git+https://github.com/juandresrodca/CompatSentinel   # PyPI package coming with the v0.1.0 release
compatsentinel diff examples/snapshots/before examples/snapshots/after
compatsentinel report examples/snapshots/before examples/snapshots/after --html report.html
```

The HTML report is a single offline file that follows your light or dark
theme. The demo data mixes real module lists from a Windows 11 capture with
fabricated apps and regressions; nothing in it identifies a real machine.

## Quick start: the Patch Tuesday workflow

1. List the apps you care about in a suite file (see
   [`examples/apps.yaml`](examples/apps.yaml)):

   ```yaml
   defaults:
     timeout_seconds: 30
     repeats: 3
   apps:
     - id: notepad
       command: notepad.exe
       window_title_regex: "Notepad"
     - id: my-lob-app
       command: 'C:\Program Files\Contoso\App.exe'
       window_title_regex: "Contoso"
       tags: [lob, dotnet]
   ```

2. Capture a baseline before you install anything:

   ```bash
   compatsentinel capture --suite apps.yaml --label before
   ```

3. Install this month's updates, reboot, then capture again with the same
   suite:

   ```bash
   compatsentinel capture --suite apps.yaml --label after
   ```

4. Diff the two and decide whether to ship the update to the rest of the
   fleet:

   ```bash
   compatsentinel diff before after --html report.html --fail-on fail
   ```

   `--fail-on fail` (the default) exits non-zero only when an app hits the
   FAIL threshold, so this drops cleanly into a scheduled task or a pipeline
   step — a WARN-level DLL version bump after a real OS update is expected
   and should not page anyone.

## What it captures, and what it deliberately does not

**Captures**, per app in your suite:

- Whether it launches, how long until its window appears, and whether it is
  still running a few seconds later.
- Loaded DLLs, their file versions, and whether they live under the Windows
  directory.
- New `Application Error` / `.NET Runtime` / `SideBySide` event log entries
  tied to the app, inside the capture window.
- New Windows Error Reporting (WER) crash reports for the app.
- The OS fingerprint: build, UBR, edition, installed KBs, .NET and VC++
runtimes.

The Application event log collector reads Error and Critical events and
attributes them to suite apps by matching their image names in the event data.
The common crash-related sources and event IDs are:

| Source | Event ID | Typical signal |
| --- | ---: | --- |
| Application Error | 1000 | Application crash and faulting module |
| .NET Runtime | 1026 | Unhandled .NET exception |
| SideBySide | varies | Manifest or assembly activation error |
| Windows Error Reporting | 1001 | Windows crash report |

These IDs describe the common event types; collection is not restricted to
this list. Events are selected by severity and capture time, then attributed
to an app from their event data.

**Deliberately does not capture**: file system or registry changes the app
makes, network activity, memory or CPU usage over time, UI screenshots, or
anything from an app not listed in your suite. CompatSentinel answers "did
this app still work after the update", not "what did this app do while it
ran". If you need file, registry or network activity, that is the scope of
the Procmon/ETW collector tracked as a
[stretch goal issue](https://github.com/juandresrodca/CompatSentinel/issues) —
not built today.

## Security and privacy model

- **Read-only and safe by design.** `capture` never modifies system state
  beyond launching and closing the exact apps in your suite file. No
  registry writes, no service changes, no installs.
- **No elevation required for the default signals.** Anything that would
  need administrator rights is optional and reported as `skipped`, never
  silently attempted.
- **No telemetry, no network calls**, anywhere in the tool — capture, diff,
  report and the MCP server all operate on local files only.
- **Diff, report and MCP run anywhere.** Only `capture` needs Windows; the
  rest is pure Python that CI runs on Ubuntu, so a contributor or a
  security reviewer never has to trust a Windows-only build step.
- **The MCP server is read-only by construction**, not just by convention:
  it does not import the code that launches or closes processes, and a test
  fails CI if that ever changes. See the [MCP section](#ask-an-ai-assistant-about-your-snapshots-mcp)
  below.

A snapshot file contains local file paths, installed KB ids, DLL versions and
raw event log text from the machine it was captured on. Treat it like any
other diagnostic export before sharing it outside your organisation.

## Ask an AI assistant about your snapshots (MCP)

`compatsentinel mcp` runs a **read-only** [Model Context Protocol](https://modelcontextprotocol.io)
server over your stored snapshots. It exposes five tools:

| Tool | Purpose |
|---|---|
| `list_snapshots` | Labels of every stored snapshot |
| `get_environment(label)` | OS build, UBR, edition, hotfixes, runtimes |
| `get_app_run(label, app_id)` | Everything captured for one app in one snapshot |
| `diff(before, after, app_id?)` | Findings, score and verdict, same engine as the CLI |
| `explain_finding(rule_id)` | What a rule id means and its default severity |

and one resource, `snapshot://{label}`, for the raw snapshot JSON.

**Security model.** The server only reads exported snapshot files under the
directory you point it at. It never imports the code that launches or closes
applications, and a test (`test_server_never_imports_the_runner`) enforces
that in CI. No tool writes to disk, calls the network, or accepts a path
outside the configured store. This mirrors
[IntuneGraph](https://github.com/juandresrodca/IntuneGraph)'s posture: the
assistant reads exported data, nothing else.

### Claude Desktop configuration

Add this to `claude_desktop_config.json` (adjust the path to your snapshots):

```json
{
  "mcpServers": {
    "compatsentinel": {
      "command": "compatsentinel",
      "args": ["mcp", "--store", "C:/path/to/your/snapshots"]
    }
  }
}
```

Or, without a pipx install, point it at the repo's virtualenv Python and use
the bundled demo data:

```json
{
  "mcpServers": {
    "compatsentinel": {
      "command": "C:/path/to/CompatSentinel/.venv/Scripts/python.exe",
      "args": ["-m", "compatsentinel", "mcp", "--store", "C:/path/to/CompatSentinel/examples/snapshots"]
    }
  }
}
```

Test any MCP server, including this one, with the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector) before
wiring it into an assistant:

```bash
npx @modelcontextprotocol/inspector compatsentinel mcp --store examples/snapshots
```

### Example conversation

Captured against the real server (`compatsentinel mcp --store examples/snapshots`)
through the MCP Inspector, using the demo snapshots from the 30-second try-it
section above.

> **You:** I have two CompatSentinel snapshots called "before" and "after".
> What changed for the app contoso-ledger?
>
> **Assistant:** *(calls `diff(before="before", after="after", app_id="contoso-ledger")`)*
>
> contoso-ledger went from **PASS** to **FAIL** (score 100/100), four findings:
>
> - **LAUNCH_FAILED** (critical) — it launched fine before; after, it exits
>   without showing its window.
> - **CRASH_NEW** (critical), twice — a WER report and an Application Error
>   event both point at `Contoso.Ledger.Native.dll`, exception `c0000005`
>   (access violation).
> - **EXIT_CODE_CHANGED** (high) — exit code `0` → `-1073741819` (`0xC0000005`,
>   the same access violation).
> - **EVENTLOG_NEW_ERRORS** (high) — a .NET Runtime 1026 event: an unhandled
>   `AccessViolationException` in `Contoso.Ledger.Native.Interop.OpenLedger`.
>
> This looks like a native interop crash triggered by the update, not a
> timing issue — the module list could not be compared because the app never
> stayed up long enough for the modules collector to run.
>
> **You:** What does CRASH_NEW mean exactly?
>
> **Assistant:** *(calls `explain_finding(rule_id="CRASH_NEW")`)*
>
> CRASH_NEW is a **critical** severity rule: it fires when a new Windows
> Error Reporting report or Application Error event is attributed to the app
> that was not present in the "before" snapshot.

## Install (development)

```bash
git clone https://github.com/juandresrodca/CompatSentinel
cd CompatSentinel
python -m venv .venv && .venv\Scripts\activate   # or: source .venv/bin/activate
python -m pip install -e ".[dev]"
pre-commit install
compatsentinel doctor
```

Everything except `capture` runs on Linux and macOS — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow.

## Troubleshooting

- **`pywin32` is unavailable:** capture uses pywin32 to access Windows APIs,
  including the Application event log. On Windows, install it into the same
  Python environment as CompatSentinel with `python -m pip install pywin32`.
  It is a Windows-only dependency; diff, report and MCP do not need it.
- **Windows Error Reporting is disabled:** `compatsentinel doctor` reports
  this on Windows. WER reports and Application Error crash events may then be
  absent, so the `CRASH_NEW` rule cannot reliably detect new crashes.
- **`capture supported: no` on Linux or macOS:** this is expected. Capture
  requires Windows 10/11; diff, report and MCP can still analyze snapshots on
  any supported OS.

## Design principles

- **Read-only and safe by design.** Never modifies system state beyond
  launching and closing the apps you list. No telemetry, no network calls.
- **Capture on Windows, analyse anywhere.** Diff, report and the MCP server
  run on Linux and macOS so contributors and CI do not need Windows.
- **Every finding explains itself.** Rule id, severity, before and after
  values, and a one-line explanation.

The reasoning behind these choices, and the Windows API quirks that shaped
them, is written up in [docs/DESIGN.md](docs/DESIGN.md).

## Roadmap

Tracked as GitHub issues, including five
[good first issues](https://github.com/juandresrodca/CompatSentinel/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
if you want to add a diff rule or a collector. Stretch goals not yet
scheduled: a Windows Sandbox runner, a Procmon/ETW file and registry
collector, Intune/winget-driven suite generation, and a GitHub Action that
posts a diff as a PR comment.

## Contributing

Bug reports, feature requests and pull requests are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and what a good
PR looks like, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for the ground
rules. Security issues go through [SECURITY.md](SECURITY.md), not a public
issue.

## License

[MIT](LICENSE)
