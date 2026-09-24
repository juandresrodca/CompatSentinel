"""Environment fingerprint: pure parsers on any OS, full collection smoke."""

from __future__ import annotations

from pytest import MonkeyPatch

from compatsentinel import environment
from compatsentinel.models import Environment

HOTFIX_OUTPUT = """
KB5044284
KB5043080

KB5039895
KB5044284
"""

DOTNET_OUTPUT = """
Microsoft.AspNetCore.App 8.0.10 [C:\\Program Files\\dotnet\\shared\\Microsoft.AspNetCore.App]
Microsoft.NETCore.App 8.0.10 [C:\\Program Files\\dotnet\\shared\\Microsoft.NETCore.App]
Microsoft.NETCore.App 6.0.33 [C:\\Program Files\\dotnet\\shared\\Microsoft.NETCore.App]
garbage line without brackets
"""

JAVA_OUTPUT = """
openjdk version "21.0.8" 2025-07-15 LTS
OpenJDK Runtime Environment Temurin-21.0.8+9 (build 21.0.8+9-LTS)
"""

NODE_OUTPUT = "v22.19.0\n"


def test_windows_11_is_detected_by_build() -> None:
    assert environment.windows_product_name(19045) == "Windows 10"
    assert environment.windows_product_name(22000) == "Windows 11"
    assert environment.windows_product_name(26100) == "Windows 11"
    assert environment.windows_product_name(None) == "Windows"


def test_hotfixes_are_unique_and_sorted_numerically() -> None:
    assert environment.parse_hotfix_output(HOTFIX_OUTPUT) == [
        "KB5039895",
        "KB5043080",
        "KB5044284",
    ]
    assert environment.parse_hotfix_output("") == []


def test_dotnet_runtimes_are_parsed() -> None:
    assert environment.parse_dotnet_runtimes(DOTNET_OUTPUT) == [
        "Microsoft.AspNetCore.App 8.0.10",
        "Microsoft.NETCore.App 6.0.33",
        "Microsoft.NETCore.App 8.0.10",
    ]


def test_java_version_is_parsed_from_stderr_format() -> None:
    assert environment.parse_java_version(JAVA_OUTPUT) == "Java 21.0.8"
    assert environment.parse_java_version("unrecognized output") is None


def test_node_version_is_parsed() -> None:
    assert environment.parse_node_version(NODE_OUTPUT) == "Node.js 22.19.0"
    assert environment.parse_node_version("unrecognized output") is None


def test_other_runtimes_are_collected_when_available(monkeypatch: MonkeyPatch) -> None:
    def which(command: str) -> str | None:
        return command if command in {"java", "node"} else None

    def run(command: list[str], *, prefer_stderr: bool = False) -> str:
        assert prefer_stderr is (command[0] == "java")
        return JAVA_OUTPUT if command[0] == "java" else NODE_OUTPUT

    monkeypatch.setattr(environment.shutil, "which", which)
    monkeypatch.setattr(environment, "_run", run)

    assert environment._other_runtimes() == ["Java 21.0.8", "Node.js 22.19.0"]


def test_other_runtimes_skip_missing_executables(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda command: None)
    assert environment._other_runtimes() == []


def test_collect_runs_on_any_os() -> None:
    env = environment.collect()
    assert isinstance(env, Environment)
    assert env.os_name
    assert env.python_version
