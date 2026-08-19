"""Pack unpack and hub-backup restore of the files_home bind (plan 6.2)."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner, Result

from gobby.cli.hub_backup import cli as hub_cli
from gobby.cli.hub_backup.files_home import (
    FILES_ARCHIVE_RELPATH,
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_MEMBERS,
    FilesHomeArchiveError,
    FilesHomeArchiveHooks,
    WalkEntry,
    files_members_would_overwrite,
    preflight_archive_graph,
    restore_files_home_from_archive,
)
from gobby.cli.pack import unpack
from gobby.config.bootstrap_io import write_bootstrap_yaml
from gobby.runner_pid_file import claim_pid_file

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class RestoreEnv:
    home: Path
    files_home: Path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RestoreEnv]:
    home = tmp_path / "gobby-home"
    files_home = tmp_path / "files-home"
    home.mkdir()
    files_home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    (tmp_path / "user-home").mkdir()
    write_bootstrap_yaml(
        home / "bootstrap.yaml",
        {
            "datastore_mode": "local",
            "files_home": str(files_home),
            "daemon_port": 60887,
            "bind_host": "127.0.0.1",
        },
    )
    yield RestoreEnv(home=home, files_home=files_home)


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _tar_info(name: str, size: int = 0, *, is_dir: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    return info


def _pack_archive(
    tmp_path: Path, files: dict[str, bytes], extra: dict[str, bytes] | None = None
) -> Path:
    archive = tmp_path / "pack.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        manifest = json.dumps({"version": 1}).encode()
        info = tarfile.TarInfo("gobby/manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        for name, payload in {**files, **(extra or {})}.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
    return archive


def _invoke_unpack(archive: Path, *args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(unpack, [str(archive), *args], input=input)


def test_6_2_3_unpack_restores_profile_attachment_and_wiki(env: RestoreEnv, tmp_path: Path) -> None:
    archive = _pack_archive(
        tmp_path,
        {
            "gobby/files/USER.md": b"profile",
            "gobby/files/_personal/attachments/p1/a.bin": b"att",
            "gobby/files/wiki/alpha/note.md": b"wiki",
        },
        extra={"gobby/bootstrap.yaml": b"datastore_mode: local\nfiles_home: /archived/files\n"},
    )
    result = _invoke_unpack(archive, "--force")
    assert result.exit_code == 0, result.output
    assert (env.files_home / "USER.md").read_text() == "profile"
    assert (env.files_home / "_personal" / "attachments" / "p1" / "a.bin").read_bytes() == b"att"
    assert (env.files_home / "wiki" / "alpha" / "note.md").read_text() == "wiki"
    dest_boot = yaml.safe_load((env.home / "bootstrap.yaml").read_text())
    assert dest_boot["files_home"] == str(env.files_home)


def test_6_2_8_and_6_2_9_preflight_limits_and_conflicts() -> None:
    assert MAX_ARCHIVE_MEMBERS == 100_000
    assert MAX_ARCHIVE_BYTES == 100 * 1024**3
    preflight_archive_graph(
        [WalkEntry(rel=str(i), is_dir=False, size=1) for i in range(MAX_ARCHIVE_MEMBERS)]
    )
    preflight_archive_graph([WalkEntry(rel="big", is_dir=False, size=MAX_ARCHIVE_BYTES)])
    with pytest.raises(FilesHomeArchiveError, match="member|100,000|limit"):
        preflight_archive_graph(
            [WalkEntry(rel=str(i), is_dir=False, size=1) for i in range(MAX_ARCHIVE_MEMBERS + 1)]
        )
    with pytest.raises(FilesHomeArchiveError, match="byte|GiB|limit"):
        preflight_archive_graph([WalkEntry(rel="big", is_dir=False, size=MAX_ARCHIVE_BYTES + 1)])
    with pytest.raises(FilesHomeArchiveError, match="duplicate"):
        preflight_archive_graph([_tar_info("gobby/files/a"), _tar_info("gobby/files/a")])
    with pytest.raises(FilesHomeArchiveError, match="prefix"):
        preflight_archive_graph([_tar_info("gobby/files/a"), _tar_info("gobby/files/a/b")])


def test_6_2_10_late_invalid_member_refuses_before_mutation(
    env: RestoreEnv, tmp_path: Path
) -> None:
    _write(env.files_home / "USER.md", "keep")
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        good = tarfile.TarInfo("gobby/files/USER.md")
        good.size = 3
        tar.addfile(good, io.BytesIO(b"new"))
        bad = tarfile.TarInfo("gobby/files/link")
        bad.type = tarfile.SYMTYPE
        bad.linkname = "USER.md"
        tar.addfile(bad)
    for extra in ([], ["--force"]):
        result = _invoke_unpack(archive, *extra)
        assert result.exit_code != 0
        assert (env.files_home / "USER.md").read_text() == "keep"
        assert not (env.files_home / "link").exists()


def test_6_2_11_valid_collision_confirm_and_force(env: RestoreEnv, tmp_path: Path) -> None:
    _write(env.files_home / "USER.md", "old")
    archive = _pack_archive(tmp_path, {"gobby/files/USER.md": b"new"})
    declined = _invoke_unpack(archive, input="n\n")
    assert declined.exit_code == 0
    assert "Aborted" in declined.output
    assert (env.files_home / "USER.md").read_text() == "old"
    confirmed = _invoke_unpack(archive, input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert (env.files_home / "USER.md").read_text() == "new"
    _write(env.files_home / "USER.md", "old")
    forced = _invoke_unpack(archive, "--force")
    assert forced.exit_code == 0, forced.output
    assert (env.files_home / "USER.md").read_text() == "new"


def test_6_2_15_missing_dest_bootstrap_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    archive = _pack_archive(tmp_path, {"gobby/files/USER.md": b"x"})
    result = _invoke_unpack(archive, "--force")
    assert result.exit_code != 0
    assert "bootstrap" in result.output.lower() or "files_home" in result.output.lower()


def test_6_2_18_restore_holds_maintenance_claim(env: RestoreEnv, tmp_path: Path) -> None:
    archive = tmp_path / "files.tar"
    with tarfile.open(archive, "w") as tar:
        payload = b"p"
        info = tarfile.TarInfo("USER.md")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    seen: list[bool] = []

    def _on_claimed(claim: object) -> None:
        del claim
        blocked = claim_pid_file(env.home / "gobby.pid", role="daemon")
        seen.append(blocked is None)
        if blocked is not None:
            blocked.release()

    restore_files_home_from_archive(
        archive,
        env.files_home,
        hooks=FilesHomeArchiveHooks(on_claimed=_on_claimed),
    )
    assert seen == [True]
    assert (env.files_home / "USER.md").read_bytes() == b"p"


def test_6_2_20_declared_size_mismatch_refuses_without_huge_alloc(
    env: RestoreEnv, tmp_path: Path
) -> None:
    archive = tmp_path / "mismatch.tar"
    with tarfile.open(archive, "w") as tar:
        payload = b"abc"
        info = tarfile.TarInfo("USER.md")
        info.size = 3
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(FilesHomeArchiveError, match="size|declared"):
        restore_files_home_from_archive(
            archive,
            env.files_home,
            hooks=FilesHomeArchiveHooks(declared_size_override=10**12),
        )
    assert not (env.files_home / "USER.md").exists()


def test_6_2_23_insufficient_space_refuses_before_mutation(env: RestoreEnv, tmp_path: Path) -> None:
    _write(env.files_home / "USER.md", "keep")
    archive = _pack_archive(tmp_path, {"gobby/files/USER.md": b"new"})
    with patch(
        "gobby.cli.hub_backup.files_home.destination_free_bytes",
        return_value=1,
    ):
        result = _invoke_unpack(archive, "--force")
    assert result.exit_code != 0
    assert "space" in result.output.lower()
    assert (env.files_home / "USER.md").read_text() == "keep"


def test_6_2_27_post_gate_swap_refuses(env: RestoreEnv, tmp_path: Path) -> None:
    archive = tmp_path / "files.tar"
    payload = b"orig"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("USER.md")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    digest = __import__("hashlib").sha256(payload).hexdigest()

    def _swap() -> None:
        archive.write_bytes(b"not-a-tar")

    with pytest.raises(FilesHomeArchiveError, match="swap|hash|identity"):
        restore_files_home_from_archive(
            archive,
            env.files_home,
            expected_sha256=digest,
            hooks=FilesHomeArchiveHooks(swap_after_confirm=_swap),
        )
    assert not (env.files_home / "USER.md").exists()


@pytest.mark.parametrize(
    "flag", ["fail_temp_write", "fail_fsync", "fail_replace", "fail_mid_member"]
)
def test_6_2_29_injected_restore_failure_preserves_dest(
    env: RestoreEnv, tmp_path: Path, flag: str
) -> None:
    _write(env.files_home / "USER.md", "prior")
    archive = tmp_path / "files.tar"
    with tarfile.open(archive, "w") as tar:
        for name, payload in (("USER.md", b"new"), ("other.txt", b"y")):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    hooks = FilesHomeArchiveHooks(
        fail_temp_write=flag == "fail_temp_write",
        fail_fsync=flag == "fail_fsync",
        fail_replace=flag == "fail_replace",
        fail_mid_member=flag == "fail_mid_member",
    )
    with pytest.raises(FilesHomeArchiveError):
        restore_files_home_from_archive(archive, env.files_home, hooks=hooks)
    assert (env.files_home / "USER.md").read_text() == "prior"
    assert not (env.files_home / "other.txt").exists()
    restore_files_home_from_archive(archive, env.files_home)
    assert (env.files_home / "USER.md").read_bytes() == b"new"
    assert (env.files_home / "other.txt").read_bytes() == b"y"


def test_files_members_would_overwrite(env: RestoreEnv) -> None:
    _write(env.files_home / "USER.md", "x")
    assert files_members_would_overwrite([_tar_info("USER.md")], env.files_home)
    assert not files_members_would_overwrite([_tar_info("missing.md")], env.files_home)


def test_hub_backup_restore_files_uses_dest_files_home(
    env: RestoreEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    archive = backup / FILES_ARCHIVE_RELPATH
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, "w") as tar:
        payload = b"hub"
        info = tarfile.TarInfo("USER.md")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    digest = __import__("hashlib").sha256(archive.read_bytes()).hexdigest()
    seen: list[Path] = []

    def _restore(source: Path, **kwargs: Any) -> dict[str, object]:
        del kwargs
        seen.append(source)
        return {"database_url": "postgresql://gobby:****@target/gobby"}

    monkeypatch.setattr(hub_cli, "restore_postgres_globals", lambda *_a, **_k: None)
    monkeypatch.setattr(hub_cli, "reconcile_restored_principals", lambda *_a, **_k: 0)
    monkeypatch.setattr(hub_cli, "restore_postgres_backup", _restore)
    monkeypatch.setattr(hub_cli, "_daemon_is_running", lambda: False)
    monkeypatch.setattr(
        hub_cli, "load_manifest", lambda _p: _fake_verified_manifest(backup, digest)
    )
    monkeypatch.setattr(hub_cli, "verify_artifacts", lambda *_a, **_k: None)
    result = CliRunner().invoke(
        hub_cli.hub_backup,
        ["restore", str(backup), "--database-url", "postgresql://gobby:x@t/gobby", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert (env.files_home / "USER.md").read_bytes() == b"hub"
    assert seen


def _fake_verified_manifest(backup: Path, digest: str) -> Any:
    from gobby.cli.hub_backup._manifest import (
        ArtifactRecord,
        HubBackupManifest,
        SourceIdentity,
        StoreRecord,
        VerificationState,
    )

    verified = VerificationState(
        verified=True, method="test", timestamp="2026-08-18T00:00:00+00:00"
    )
    store = StoreRecord(archive_verified=verified, restore_verified=verified, details={})
    return HubBackupManifest(
        created_at="2026-08-18T00:00:00+00:00",
        gobby_version="0.5.0",
        epoch_id=None,
        source_identity=SourceIdentity("1", "gobby", 1),
        backup_starting_head=1,
        row_count_probes={},
        artifacts=[
            ArtifactRecord(
                name="files-home",
                path=FILES_ARCHIVE_RELPATH,
                sha256=digest,
                size_bytes=0,
            )
        ],
        stores=dict.fromkeys(("postgres", "qdrant", "falkordb", "volumes", "files"), store),
    )
