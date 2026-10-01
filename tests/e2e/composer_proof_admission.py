"""Explicit, secret-free authorization/configuration for the opt-in proof."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tests.e2e.composer_proof import ProofRefused


class Binary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Provider(Binary):
    auth_reference: Path


class Admission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    grant: Literal["PD_AUTHORIZED_ISOLATED_EXECUTION"]
    reviewed_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    reviewed_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    issued_at: datetime
    exclusion_message_id: UUID
    complete_exclusion_map: Literal[True]
    excluded_identities: frozenset[UUID] = Field(min_length=26)
    native: dict[str, Binary]
    providers: dict[Literal["claude", "codex"], Provider]
    support_tools: list[Binary]
    artifact_dir: Path | None = None
    worktree_test_notice_id: UUID | None = None

    def verify(self, *, now: datetime, commit: str, tree: str) -> None:
        if (
            self.issued_at.tzinfo is None
            or not -30 <= (now - self.issued_at).total_seconds() <= 300
        ):
            raise ProofRefused("fresh complete exclusion map required")
        if commit != self.reviewed_commit or tree != self.reviewed_tree:
            raise ProofRefused("reviewed source identity mismatch")
        if set(self.providers) != {"claude", "codex"} or set(self.native) != {
            "gcode",
            "gdaemon",
            "ghook",
            "gterm",
            "gclient",
        }:
            raise ProofRefused("complete provider and native binary specifications required")


def git_read(checkout: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=checkout, text=True, timeout=10).strip()


def load_admission(path: Path, checkout: Path) -> Admission:
    if path.is_symlink():
        raise ProofRefused("admission must not be a symlink")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not path.is_file():
        raise ProofRefused("admission must be an owner-private configuration file")
    try:
        spec = Admission.model_validate_json(path.read_bytes())
    except ValidationError:
        raise ProofRefused("invalid secret-free admission shape") from None
    spec.verify(
        now=datetime.now(UTC),
        commit=git_read(checkout, "rev-parse", "HEAD"),
        tree=git_read(checkout, "rev-parse", "HEAD^{tree}"),
    )
    if git_read(checkout, "status", "--porcelain"):
        raise ProofRefused("proof requires a clean reviewed source checkout")
    return spec


def verify_binary_set(spec: Admission) -> Path:
    """Only called after execution admission; never builds or installs a binary."""
    from gobby.install.bin_set_coherence import (
        SET_MEMBERS,
        _require_installed_agreement,
        probe_set_member_identity,
    )

    for binary in (*spec.native.values(), *spec.providers.values(), *spec.support_tools):
        if not binary.path.is_absolute() or not os.access(binary.path, os.X_OK):
            raise ProofRefused("admitted executable is unavailable")
        with binary.path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != binary.sha256:
            raise ProofRefused("admitted executable bytes changed")
    bin_dir = spec.native["gdaemon"].path.parent
    if any(
        binary.path.parent != bin_dir or binary.path.name != name
        for name, binary in spec.native.items()
    ):
        raise ProofRefused("native binaries must share the admitted installed directory")
    identities = {
        name: probe_set_member_identity(spec.native[name].path, name) for name in SET_MEMBERS
    }
    _require_installed_agreement(identities, bin_dir=bin_dir)
    return bin_dir


def binary_readback(spec: Admission, env: dict[str, str]) -> list[dict[str, object]]:
    if env.get("GOBBY_COMPOSER_PROOF_EXECUTION") != spec.grant:
        raise ProofRefused("execution grant required for version readback")
    verify_binary_set(spec)
    entries: list[dict[str, object]] = []
    binaries: dict[str, Binary] = dict(spec.native)
    for provider_name, provider_binary in spec.providers.items():
        binaries[provider_name] = provider_binary
    for name, binary in binaries.items():
        info = binary.path.stat()
        completed = subprocess.run(
            [str(binary.path), "--version"], env=env, capture_output=True, check=True, timeout=10
        )
        match = re.search(rb"\b\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.]+)?", completed.stdout)
        if match is None:
            raise ProofRefused("admitted executable version is unconfirmed")
        entries.append(
            {
                "name": name,
                "path": str(binary.path),
                "sha256": binary.sha256,
                "device": info.st_dev,
                "inode": info.st_ino,
                "version": match.group().decode("ascii"),
            }
        )
    for binary in spec.support_tools:
        info = binary.path.stat()
        entries.append(
            {
                "name": "support",
                "path": str(binary.path),
                "sha256": binary.sha256,
                "device": info.st_dev,
                "inode": info.st_ino,
            }
        )
    return entries
