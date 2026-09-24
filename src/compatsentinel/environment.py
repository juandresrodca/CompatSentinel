"""OS fingerprint: build, UBR, edition, installed KBs, and common runtimes.

Everything is read from the registry or from read-only commands
(``Get-HotFix``, ``dotnet --list-runtimes``). A source that fails is logged and
left empty; the fingerprint is context for findings, so a partial one must
never abort a capture.

The Windows-only pieces import ``winreg`` inside the function so the module
imports cleanly on Linux and macOS, where only :func:`collect` is used to
describe the host running ``diff``.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
import sys

from compatsentinel.models import Environment

log = logging.getLogger(__name__)

CURRENT_VERSION_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
NET_FRAMEWORK_KEY = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
UNINSTALL_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)
WINDOWS_11_FIRST_BUILD = 22000
COMMAND_TIMEOUT_SECONDS = 60

_KB_LINE = re.compile(r"^KB\d+$")
_DOTNET_RUNTIME_LINE = re.compile(r"^(?P<name>\S+)\s+(?P<version>\S+)\s+\[")
_JAVA_VERSION_LINE = re.compile(r'^(?:openjdk|java) version "(?P<version>[^"]+)"')
_NODE_VERSION_LINE = re.compile(r"^v?(?P<version>\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?)$")


def collect() -> Environment:
    """Fingerprint the current host."""
    if sys.platform == "win32":
        return _collect_windows()
    else:
        return Environment(
            os_name=platform.system(),
            os_version=platform.release(),
            architecture=platform.machine(),
            python_version=platform.python_version(),
        )


# --- Pure helpers (unit tested on any OS) -------------------------------------


def windows_product_name(build: int | None) -> str:
    """Windows 11 shipped as build 22000; the registry still says "Windows 10" there.

    ``ProductName`` and ``platform.release()`` both report 10 on Windows 11, so
    the build number is the only reliable discriminator.
    """
    if build is None:
        return "Windows"
    return "Windows 11" if build >= WINDOWS_11_FIRST_BUILD else "Windows 10"


def parse_hotfix_output(text: str) -> list[str]:
    """Extract sorted, unique KB ids from ``Get-HotFix`` output."""
    ids = {line.strip() for line in text.splitlines() if _KB_LINE.match(line.strip())}
    return sorted(ids, key=lambda kb: int(kb[2:]))


def parse_dotnet_runtimes(text: str) -> list[str]:
    """Turn ``dotnet --list-runtimes`` lines into ``"Name Version"`` strings."""
    runtimes: list[str] = []
    for line in text.splitlines():
        if match := _DOTNET_RUNTIME_LINE.match(line.strip()):
            runtimes.append(f"{match['name']} {match['version']}")
    return sorted(set(runtimes))


def parse_java_version(text: str) -> str | None:
    """Extract the runtime version from the first line of ``java -version``."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if match := _JAVA_VERSION_LINE.match(first_line.strip()):
        return f"Java {match['version']}"
    return None


def parse_node_version(text: str) -> str | None:
    """Extract a Node.js version such as ``v22.19.0``."""
    if match := _NODE_VERSION_LINE.match(text.strip()):
        return f"Node.js {match['version']}"
    return None


def _to_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


# --- Windows sources -------------------------------------------------------------


def _collect_windows() -> Environment:
    values = _read_registry_values(
        CURRENT_VERSION_KEY, ("CurrentBuild", "UBR", "DisplayVersion", "EditionID")
    )
    build = _to_int(values.get("CurrentBuild"))
    return Environment(
        os_name=windows_product_name(build),
        os_version=platform.version(),
        architecture=platform.machine(),
        python_version=platform.python_version(),
        build=build,
        ubr=_to_int(values.get("UBR")),
        display_version=_as_str(values.get("DisplayVersion")),
        edition=_as_str(values.get("EditionID")),
        hotfixes=_installed_hotfixes(),
        dotnet_runtimes=_dotnet_runtimes(),
        vcpp_runtimes=_vcpp_runtimes(),
        other_runtimes=_other_runtimes(),
    )


def _as_str(value: object) -> str | None:
    return None if value is None else str(value)


def _read_registry_values(key_path: str, names: tuple[str, ...]) -> dict[str, object]:
    """Read named values under HKLM; missing keys or values are simply absent."""
    import winreg

    found: dict[str, object] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            for name in names:
                try:
                    found[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    continue
    except OSError as exc:
        log.warning("cannot open HKLM\\%s: %s", key_path, exc)
    return found


def _run(command: list[str], *, prefer_stderr: bool = False) -> str:
    """Run a read-only command and return stdout, or "" when it fails."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("%s failed: %s", command[0], exc)
        return ""
    if completed.returncode != 0:
        log.warning("%s exited %d: %s", command[0], completed.returncode, completed.stderr.strip())
    if prefer_stderr and completed.stderr.strip():
        return completed.stderr
    return completed.stdout


def _installed_hotfixes() -> list[str]:
    # Get-HotFix wraps Win32_QuickFixEngineering, the same source as the old wmic qfe.
    output = _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-HotFix | ForEach-Object HotFixID",
        ]
    )
    return parse_hotfix_output(output)


def _dotnet_runtimes() -> list[str]:
    runtimes: list[str] = []
    framework = _read_registry_values(NET_FRAMEWORK_KEY, ("Version",)).get("Version")
    if framework:
        runtimes.append(f".NET Framework {framework}")
    if shutil.which("dotnet"):
        runtimes.extend(parse_dotnet_runtimes(_run(["dotnet", "--list-runtimes"])))
    return runtimes


def _other_runtimes() -> list[str]:
    """Java and Node.js versions available on ``PATH``."""
    runtimes: list[str] = []
    if shutil.which("java"):
        java = parse_java_version(_run(["java", "-version"], prefer_stderr=True))
        if java:
            runtimes.append(java)
    if shutil.which("node"):
        node = parse_node_version(_run(["node", "--version"]))
        if node:
            runtimes.append(node)
    return runtimes


def _vcpp_runtimes() -> list[str]:
    """Visual C++ redistributables, from the Add/Remove Programs registry entries."""
    import winreg

    names: set[str] = set()
    for base in UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    sub_name = winreg.EnumKey(key, index)
                    display = _read_registry_values(f"{base}\\{sub_name}", ("DisplayName",))
                    name = str(display.get("DisplayName", ""))
                    if "Visual C++" in name and "Redistributable" in name:
                        names.add(" ".join(name.split()))
        except OSError as exc:
            log.warning("cannot enumerate HKLM\\%s: %s", base, exc)
    return sorted(names)
