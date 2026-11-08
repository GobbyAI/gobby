"""Handoff-specific admission; independent of the composer proof's auth contract."""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tests.e2e import composer_proof_admission as _binary
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_admission import Binary

PROVIDERS = ("claude", "codex", "grok", "agy", "droid")


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
    providers: dict[str, Binary]
    support_tools: list[Binary]
    artifact_dir: Path
    worktree_test_notice_id: UUID


def load_admission(path: Path, checkout: Path) -> Admission:
    if path.is_symlink():
        raise ProofRefused("admission must not be a symlink")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not path.is_file():
        raise ProofRefused("admission must be an owner-private configuration file")
    try:
        spec = Admission.model_validate_json(path.read_bytes())
    except ValidationError:
        raise ProofRefused("invalid secret-free handoff admission shape") from None
    if (
        spec.issued_at.tzinfo is None
        or not -30 <= (datetime.now(UTC) - spec.issued_at).total_seconds() <= 300
    ):
        raise ProofRefused("fresh complete exclusion map required")
    for provider in PROVIDERS:
        if provider not in spec.providers:
            raise ProofRefused(f"{provider}: provider executable admission missing")
    if set(spec.providers) != set(PROVIDERS) or set(spec.native) != {
        "gcode",
        "gdaemon",
        "ghook",
        "gterm",
        "gclient",
    }:
        raise ProofRefused("complete provider and native specifications required")
    if (
        _binary.git_read(checkout, "rev-parse", "HEAD") != spec.reviewed_commit
        or _binary.git_read(checkout, "rev-parse", "HEAD^{tree}") != spec.reviewed_tree
        or _binary.git_read(checkout, "status", "--porcelain")
    ):
        raise ProofRefused("clean reviewed source identity required")
    return spec


def verify_binary_set(spec: Admission) -> Path:
    from gobby.install.bin_set_coherence import (
        _require_installed_agreement,
        probe_set_member_identity,
    )
    from gobby.utils.native_bin import SET_MEMBERS

    binaries = {**spec.native, **spec.providers}
    binaries.update({f"support-{i}": item for i, item in enumerate(spec.support_tools)})
    # Check provider availability first so a missing CLI is always named.
    for name, binary in [*spec.providers.items(), *binaries.items()]:
        if not binary.path.is_absolute() or not os.access(binary.path, os.X_OK):
            raise ProofRefused(f"{name}: admitted executable is unavailable")
        with binary.path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != binary.sha256:
            raise ProofRefused(f"{name}: admitted executable bytes changed")
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


def binary_readback(
    spec: Admission, env: dict[str, str], provider_env: dict[str, str]
) -> list[dict[str, object]]:
    verify_binary_set(spec)
    entries: list[dict[str, object]] = []
    for name, binary in {**spec.native, **spec.providers}.items():
        try:
            result = subprocess.run(
                [str(binary.path), "--version"],
                env=provider_env if name in spec.providers else env,
                capture_output=True,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise ProofRefused(f"{name}: executable version probe failed") from None
        entries.append(
            {
                "name": name,
                "path": str(binary.path),
                "sha256": binary.sha256,
                "version": result.stdout.decode(errors="replace").strip(),
            }
        )
    return entries
