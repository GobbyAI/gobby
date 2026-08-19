"""Tests for the gobby-hub-backup-manifest v3 contract and destructive gate."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.cli.hub_backup._manifest import (
    MANIFEST_FORMAT,
    MANIFEST_VERSION,
    ArtifactRecord,
    GateDecision,
    HubBackupManifest,
    SourceIdentity,
    StoreRecord,
    VerificationState,
    check_manifest_gate,
    load_manifest,
    write_manifest,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
_ARTIFACT_CONTENT = b"complete-postgres-dump"


def _backup_root(tmp_path: Path) -> Path:
    backup_root = tmp_path / "backup"
    artifact_path = backup_root / "postgres" / "gobby.dump"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(_ARTIFACT_CONTENT)
    return backup_root


def _identity(**overrides: object) -> SourceIdentity:
    values: dict[str, object] = {
        "pg_system_identifier": "7310558436471271234",
        "database_name": "gobby",
        "database_oid": 16384,
    }
    values.update(overrides)
    return SourceIdentity(**values)  # type: ignore[arg-type]


def _verified(method: str, *, at: datetime = _NOW) -> VerificationState:
    return VerificationState(verified=True, method=method, timestamp=at.isoformat())


def _unverified() -> VerificationState:
    return VerificationState(verified=False, method=None, timestamp=None)


def _store(*, restore_verified: bool = True) -> StoreRecord:
    return StoreRecord(
        archive_verified=_verified("sha256"),
        restore_verified=(_verified("scratch-restore") if restore_verified else _unverified()),
        details={},
    )


def _manifest(
    *,
    created_at: datetime = _NOW,
    identity: SourceIdentity | None = None,
    restore_verified: bool = True,
    epoch_id: str | None = None,
) -> HubBackupManifest:
    return HubBackupManifest(
        created_at=created_at.isoformat(),
        gobby_version="0.5.0",
        epoch_id=epoch_id,
        source_identity=identity or _identity(),
        backup_starting_head=353,
        row_count_probes={"tasks": 19388, "sessions": 9930},
        artifacts=[
            ArtifactRecord(
                name="postgres-dump",
                path="postgres/gobby.dump",
                sha256=hashlib.sha256(_ARTIFACT_CONTENT).hexdigest(),
                size_bytes=len(_ARTIFACT_CONTENT),
            )
        ],
        stores={
            "postgres": _store(restore_verified=restore_verified),
            "qdrant": _store(),
            "falkordb": _store(),
            "volumes": _store(),
            "files": _store(),
        },
    )


class TestManifestRoundTrip:
    def test_write_then_load_preserves_contract_fields(self, tmp_path: Path) -> None:
        manifest = _manifest(epoch_id="epoch-123")
        path = tmp_path / "manifest.json"

        write_manifest(manifest, path)
        loaded = load_manifest(path)

        assert loaded.manifest_format == MANIFEST_FORMAT == "gobby-hub-backup-manifest"
        assert loaded.manifest_version == MANIFEST_VERSION == 3
        assert loaded.epoch_id == "epoch-123"
        assert loaded.source_identity == _identity()
        assert loaded.backup_starting_head == 353
        assert loaded.row_count_probes["tasks"] == 19388
        assert loaded.artifacts[0].sha256 == hashlib.sha256(_ARTIFACT_CONTENT).hexdigest()
        assert loaded.stores["postgres"].restore_verified.verified is True
        assert loaded.stores["postgres"].restore_verified.method == "scratch-restore"

    def test_written_file_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        write_manifest(_manifest(), path)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_load_rejects_wrong_format(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        write_manifest(_manifest(), path)
        raw = path.read_text(encoding="utf-8").replace(
            "gobby-hub-backup-manifest", "some-other-format"
        )
        path.write_text(raw, encoding="utf-8")

        with pytest.raises(ValueError, match="manifest_format"):
            load_manifest(path)

    def test_load_rejects_v1_manifest(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        write_manifest(_manifest(), path)
        raw = path.read_text(encoding="utf-8").replace(
            '"manifest_version": 3', '"manifest_version": 1'
        )
        path.write_text(raw, encoding="utf-8")

        with pytest.raises(ValueError, match="manifest_version"):
            load_manifest(path)

    def test_load_rejects_missing_required_field(self, tmp_path: Path) -> None:
        import json

        path = tmp_path / "manifest.json"
        write_manifest(_manifest(), path)
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["backup_starting_head"]
        path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ValueError, match="backup_starting_head"):
            load_manifest(path)


class TestManifestGate:
    def test_accepts_fresh_fully_verified_manifest(self, tmp_path: Path) -> None:
        decision = check_manifest_gate(
            _manifest(),
            backup_root=_backup_root(tmp_path),
            current_identity=_identity(),
            now=_NOW + timedelta(hours=1),
        )
        assert decision == GateDecision(ok=True, reasons=[])

    def test_refuses_manifest_older_than_max_age(self, tmp_path: Path) -> None:
        decision = check_manifest_gate(
            _manifest(created_at=_NOW - timedelta(hours=25)),
            backup_root=_backup_root(tmp_path),
            current_identity=_identity(),
            now=_NOW,
        )
        assert decision.ok is False
        assert any("max age" in reason for reason in decision.reasons)

    def test_max_age_is_configurable(self, tmp_path: Path) -> None:
        manifest = _manifest(created_at=_NOW - timedelta(hours=2))
        backup_root = _backup_root(tmp_path)
        refused = check_manifest_gate(
            manifest,
            backup_root=backup_root,
            current_identity=_identity(),
            now=_NOW,
            max_age_hours=1,
        )
        accepted = check_manifest_gate(
            manifest,
            backup_root=backup_root,
            current_identity=_identity(),
            now=_NOW,
            max_age_hours=3,
        )
        assert refused.ok is False
        assert accepted.ok is True

    def test_refuses_missing_or_unverified_files_store(self, tmp_path: Path) -> None:
        backup_root = _backup_root(tmp_path)
        manifest = _manifest()
        manifest.stores.pop("files")
        missing = check_manifest_gate(
            manifest,
            backup_root=backup_root,
            current_identity=_identity(),
            now=_NOW,
        )
        assert missing.ok is False
        assert any("files" in reason for reason in missing.reasons)

        unverified_manifest = _manifest()
        unverified_manifest.stores["files"] = _store(restore_verified=False)
        refused = check_manifest_gate(
            unverified_manifest,
            backup_root=backup_root,
            current_identity=_identity(),
            now=_NOW,
        )
        assert refused.ok is False
        assert any("files" in reason for reason in refused.reasons)

    def test_refuses_manifest_lacking_restore_verified(self, tmp_path: Path) -> None:
        decision = check_manifest_gate(
            _manifest(restore_verified=False),
            backup_root=_backup_root(tmp_path),
            current_identity=_identity(),
            now=_NOW,
        )
        assert decision.ok is False
        assert any("restore_verified" in reason for reason in decision.reasons)

    def test_refuses_fingerprint_mismatch_on_database_oid(self, tmp_path: Path) -> None:
        decision = check_manifest_gate(
            _manifest(identity=_identity(database_oid=99999)),
            backup_root=_backup_root(tmp_path),
            current_identity=_identity(),
            now=_NOW,
        )
        assert decision.ok is False
        assert any("identity" in reason for reason in decision.reasons)

    def test_refuses_fingerprint_mismatch_on_system_identifier(self, tmp_path: Path) -> None:
        decision = check_manifest_gate(
            _manifest(identity=_identity(pg_system_identifier="1111111111")),
            backup_root=_backup_root(tmp_path),
            current_identity=_identity(),
            now=_NOW,
        )
        assert decision.ok is False
        assert any("identity" in reason for reason in decision.reasons)

    def test_collects_every_refusal_reason(self, tmp_path: Path) -> None:
        decision = check_manifest_gate(
            _manifest(
                created_at=_NOW - timedelta(hours=48),
                identity=_identity(database_name="other"),
                restore_verified=False,
            ),
            backup_root=_backup_root(tmp_path),
            current_identity=_identity(),
            now=_NOW,
        )
        assert decision.ok is False
        assert len(decision.reasons) == 3

    def test_refuses_artifact_mutated_after_manifest_verification(self, tmp_path: Path) -> None:
        backup_root = _backup_root(tmp_path)
        (backup_root / "postgres" / "gobby.dump").write_bytes(b"mutated-after-verification")

        decision = check_manifest_gate(
            _manifest(),
            backup_root=backup_root,
            current_identity=_identity(),
            now=_NOW,
        )

        assert decision.ok is False
        assert any("postgres/gobby.dump" in reason for reason in decision.reasons)
        assert any("sha256" in reason for reason in decision.reasons)
