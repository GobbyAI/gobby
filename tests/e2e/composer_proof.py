"""Opt-in, isolated real-provider acceptance support for composer safety."""

from __future__ import annotations

import asyncio
import hashlib
import os
import pwd
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


def require_private_root(root: Path) -> Path:
    try:
        info = root.lstat()
        if root.is_symlink():
            raise ProofRefused("proof root must not be a symlink")
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProofRefused("proof root must be an owner-private directory")
        resolved = root.resolve(strict=True)
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        if resolved in {Path("/"), account_home}:
            raise ProofRefused("shared home is not a proof root")
        return resolved
    except OSError as exc:
        raise ProofRefused("proof root cannot be verified") from exc


def sealed_environment(ambient: dict[str, str], *, home: Path, native_bin: Path) -> dict[str, str]:
    environment = {key: ambient[key] for key in ("TERM", "LANG", "LC_ALL") if key in ambient}
    environment.update(
        HOME=str(home),
        GOBBY_HOME=str(home),
        GOBBY_TEST_PROTECT="1",
        GOBBY_NATIVE_BIN_DIR=str(native_bin),
        TMPDIR=str(home / "tmp"),
        PATH=f"{native_bin}{os.pathsep}/usr/bin:/bin:/usr/sbin:/sbin",
    )
    return environment


class ProofRefused(RuntimeError):
    """A proof cannot establish the required isolation or evidence."""


@dataclass(frozen=True)
class Surface:
    provider: str
    session_id: str
    terminal_id: str
    host_epoch: str
    project_id: str


@dataclass(frozen=True)
class ProofScope:
    root: Path
    project_id: str
    excluded: frozenset[str]
    source_commit: str

    def require_surface(self, expected: Surface, observed: Surface) -> None:
        if expected != observed or not expected.host_epoch:
            raise ProofRefused("terminal binding changed")
        if expected.project_id != self.project_id:
            raise ProofRefused("foreign project")
        if not self.excluded or {expected.session_id, expected.terminal_id} & self.excluded:
            raise ProofRefused("missing exclusion map or excluded identity")
        if expected.provider not in {"claude", "codex"}:
            raise ProofRefused("unsupported provider")
        for value in (expected.session_id, expected.terminal_id, self.project_id):
            try:
                UUID(value)
            except ValueError as exc:
                raise ProofRefused("invalid binding identity") from exc


def owned_editor_cleanup(provider: str, text: str, cursor_from_end: int) -> str:
    if provider not in {"claude", "codex"}:
        raise ProofRefused("unsupported provider")
    if text != f"R2_22915_{provider.upper()}_DRAFT_ABCD_EFGH":
        raise ProofRefused("draft is not the exact synthetic owned draft")
    if cursor_from_end != 5:
        raise ProofRefused("cursor differs from the recorded owned position")
    return "\x1b[C" * 5 + "\x7f" * len(text)


def link_existing_auth(root: Path, provider: str, source: Path) -> Path:
    require_private_root(root)
    names = {"claude": ".credentials.json", "codex": "auth.json"}
    if provider not in names:
        raise ProofRefused("unsupported provider")
    original = source.resolve(strict=True)
    info = original.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProofRefused("existing auth must be an owner-private regular file")
    directory = root / provider
    if directory.is_symlink():
        raise ProofRefused("provider config must stay inside the proof root")
    directory.mkdir(mode=0o700, exist_ok=True)
    require_private_root(directory)
    destination = directory / names[provider]
    if destination.exists() or destination.is_symlink():
        raise ProofRefused("auth destination already exists")
    destination.symlink_to(original)
    return destination


def safe_evidence(surface: Surface, frame: str, cursor: tuple[int, int]) -> dict[str, object]:
    return {
        "provider": surface.provider,
        "session_id": surface.session_id,
        "terminal_id": surface.terminal_id,
        "host_epoch": surface.host_epoch,
        "project_id": surface.project_id,
        "frame_sha256": hashlib.sha256(frame.encode()).hexdigest(),
        "cursor": list(cursor),
    }


def require_execution(scope: ProofScope, approved_commit: str, live_opt_in: str) -> None:
    if live_opt_in != "PD_AUTHORIZED_ISOLATED_EXECUTION":
        raise ProofRefused("explicit live execution authorization is required")
    if approved_commit != scope.source_commit or len(approved_commit) != 40:
        raise ProofRefused("reviewed source commit differs")
    try:
        int(approved_commit, 16)
    except ValueError as exc:
        raise ProofRefused("invalid source commit") from exc


async def cleanup_surfaces(
    scope: ProofScope,
    surfaces: list[Surface],
    read: Callable[[Surface], Awaitable[Surface]],
    terminate: Callable[[Surface], Awaitable[None]],
    retries: list[asyncio.Task[None]],
) -> None:
    for retry in retries:
        retry.cancel()
    await asyncio.gather(*retries, return_exceptions=True)
    refused: ProofRefused | None = None
    for surface in surfaces:
        try:
            scope.require_surface(surface, await read(surface))
        except ProofRefused as exc:
            refused = exc
            continue
        await asyncio.wait_for(terminate(surface), timeout=10)
    if refused is not None:
        raise refused
