"""Resolve, invoke, and parse mypy without depending on Click."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Final

MYPY_TIMEOUT_SECONDS: Final = 900
_OUTPUT_LIMIT: Final = 4_000
_MYPY_FLAGS: Final = (
    "--show-error-codes",
    "--no-error-summary",
    "--no-color-output",
    "--no-pretty",
)
_ERROR_RE: Final = re.compile(
    r"^(?P<path>.+?):(?P<location>\d+(?::\d+){0,3}): error: (?P<body>.+)$"
)
_CODE_RE: Final = re.compile(r"\s+\[(?P<code>[^\[\]]+)\]\s*$")


@dataclass(frozen=True, slots=True)
class MypyDiagnostic:
    """One parsed mypy error with a project-relative stable path."""

    path: str
    line: int
    code: str
    message: str


class MypyInvocationError(RuntimeError):
    """Raised when mypy could not produce a trustworthy diagnostic report."""

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        self.stdout = _truncate(stdout)
        self.stderr = _truncate(stderr)
        details = [message]
        if self.stdout:
            details.append(f"stdout:\n{self.stdout}")
        if self.stderr:
            details.append(f"stderr:\n{self.stderr}")
        super().__init__("\n".join(details))


def resolve_mypy_command(root: Path, override: str | None = None) -> tuple[str, ...]:
    """Resolve mypy in the target project, pinning package managers to their markers."""

    if override is not None:
        command = tuple(shlex.split(override))
        if not command:
            raise MypyInvocationError("--mypy-command must not be empty")
        return command

    package_managers = (
        ("uv.lock", "uv", ("uv", "run", "mypy")),
        ("poetry.lock", "poetry", ("poetry", "run", "mypy")),
        ("pdm.lock", "pdm", ("pdm", "run", "mypy")),
    )
    for marker, executable, command in package_managers:
        if (root / marker).exists() and shutil.which(executable) is not None:
            return command

    mypy_path = shutil.which("mypy")
    if mypy_path is not None:
        return (mypy_path,)
    return (sys.executable, "-m", "mypy")


def normalize_reported_path(reported_path: str, *, root: Path) -> str:
    """Normalize mypy paths to root-relative POSIX form, including raw backslashes."""

    root_resolved = root.resolve()
    if "\\" in reported_path:
        windows_path = PureWindowsPath(reported_path)
        if windows_path.is_absolute():
            if os.name != "nt":
                raise ValueError(f"mypy reported a path outside the project root: {reported_path}")
            candidate = Path(reported_path)
        else:
            candidate = root_resolved.joinpath(*windows_path.parts)
    else:
        candidate_path = Path(reported_path)
        candidate = (
            candidate_path if candidate_path.is_absolute() else root_resolved / candidate_path
        )

    candidate_resolved = candidate.resolve()
    try:
        relative = candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"mypy reported a path outside the project root: {reported_path}") from exc
    return relative.as_posix()


def parse_mypy_output(output: str, *, root: Path) -> tuple[MypyDiagnostic, ...]:
    """Parse mypy error lines while ignoring notes and summary text."""

    diagnostics: list[MypyDiagnostic] = []
    for line in output.splitlines():
        match = _ERROR_RE.match(line)
        if match is None:
            continue
        body = match.group("body")
        code_match = _CODE_RE.search(body)
        code = code_match.group("code") if code_match is not None else "unknown"
        message = body[: code_match.start()].rstrip() if code_match is not None else body
        try:
            path = normalize_reported_path(match.group("path"), root=root)
        except ValueError:
            continue
        diagnostics.append(
            MypyDiagnostic(
                path=path,
                line=int(match.group("location").split(":", maxsplit=1)[0]),
                code=f"mypy:{code}",
                message=message,
            )
        )
    return tuple(diagnostics)


def run_mypy(
    targets: tuple[str, ...],
    *,
    root: Path,
    mypy_command: str | None = None,
    timeout: int = MYPY_TIMEOUT_SECONDS,
) -> tuple[MypyDiagnostic, ...]:
    """Run mypy and return parsed findings, distinguishing findings from invocation errors."""

    command = [*resolve_mypy_command(root, mypy_command), *_MYPY_FLAGS, *targets]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MypyInvocationError(f"mypy executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MypyInvocationError(
            f"mypy timed out after {timeout} seconds",
            stdout=_stream_text(exc.stdout),
            stderr=_stream_text(exc.stderr),
        ) from exc

    if completed.returncode not in {0, 1}:
        raise MypyInvocationError(
            f"mypy failed with exit code {completed.returncode}",
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    has_error_line = any(
        _ERROR_RE.match(line) is not None for line in completed.stdout.splitlines()
    )
    try:
        diagnostics = parse_mypy_output(completed.stdout, root=root)
    except ValueError as exc:
        raise MypyInvocationError(
            str(exc),
            stdout=completed.stdout,
            stderr=completed.stderr,
        ) from exc
    if completed.returncode == 1 and not has_error_line:
        raise MypyInvocationError(
            "mypy exit code 1 produced no parseable errors",
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return diagnostics


def _stream_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _truncate(value: str) -> str:
    if len(value) <= _OUTPUT_LIMIT:
        return value.strip()
    omitted = len(value) - _OUTPUT_LIMIT
    return f"{value[:_OUTPUT_LIMIT].rstrip()}\n... truncated {omitted} characters"
