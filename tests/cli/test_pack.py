"""Tests for gobby pack and unpack CLI commands."""

from __future__ import annotations

import io
import json
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.pack import _human_size, _import_docker_volume, pack, unpack
from gobby.config.bootstrap_io import write_bootstrap_yaml

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestPackHelpers:
    def test_human_size(self) -> None:
        assert _human_size(500) == "500B"
        assert _human_size(1024) == "1.0KB"
        assert _human_size(1048576) == "1.0MB"
        assert _human_size(1073741824) == "1.0GB"
        assert _human_size(1099511627776) == "1.0TB"
        assert _human_size(1649267441664) == "1.5TB"

    @patch("gobby.cli.pack.subprocess.run")
    def test_volume_import_passes_archive_name_as_positional_argument(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        archive = tmp_path / "backup;not-shell.tar.gz"

        result = _import_docker_volume("gobby_data", archive)

        assert result is True
        assert mock_run.call_count == 2
        command = mock_run.call_args.args[0]
        assert "$1" in command[-3]
        assert archive.name not in command[-3]
        assert command[-1] == archive.name


class TestPackCommand:
    @patch("gobby.cli.pack.get_gobby_home")
    def test_pack_no_gobby_home(self, mock_home, runner: CliRunner) -> None:
        fake_path = MagicMock()
        fake_path.exists.return_value = False
        mock_home.return_value = fake_path
        result = runner.invoke(pack, [])
        assert result.exit_code == 1
        assert "No ~/.gobby directory found" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    def test_pack_dry_run(
        self, mock_docker, mock_daemon, mock_home, tmp_path, runner: CliRunner
    ) -> None:
        # Setup fake GOBBY_HOME structure
        fake_home = tmp_path / ".gobby"
        fake_home.mkdir()
        (fake_home / "bootstrap.yaml").write_text("hub_backend: postgres\n")
        (fake_home / "local_cli_token").write_text("token\n")
        (fake_home / "session_transcripts").mkdir()
        (fake_home / "session_transcripts" / "1.txt").write_text("ts")

        mock_home.return_value = fake_home

        result = runner.invoke(pack, ["--dry-run"])
        assert result.exit_code == 0
        assert "Pack contents (dry run):" in result.output
        assert "gobby/bootstrap.yaml" in result.output
        assert "gobby/hub-postgres.db" not in result.output
        assert "gobby/local_cli_token" not in result.output
        assert "gobby/session_transcripts/" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    def test_pack_success(
        self, mock_docker, mock_daemon, mock_home, tmp_path, runner: CliRunner
    ) -> None:
        fake_home = tmp_path / ".gobby"
        fake_home.mkdir()
        (fake_home / "bootstrap.yaml").write_text("hub_backend: postgres\n")
        (fake_home / "local_cli_token").write_text("token\n")
        (fake_home / ".secret_kek").write_text("kek-key\n")

        mock_home.return_value = fake_home

        out_path = tmp_path / "out.tar.gz"
        result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code == 0
        assert "Packing Gobby data" in result.output
        assert "Warning: pack archives contain secrets" in result.output
        assert out_path.exists()
        assert out_path.stat().st_mode & 0o777 == 0o600

        # Verify tarball
        with tarfile.open(out_path, "r:gz") as tar:
            names = tar.getnames()
            assert "gobby/manifest.json" in names
            assert "gobby/bootstrap.yaml" in names
            assert "gobby/.secret_salt" not in names
            assert "gobby/.secret_kek" in names
            assert "gobby/hub-postgres.db" not in names
            assert "gobby/local_cli_token" not in names

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    @patch("gobby.cli.pack.postgres_backup_configured", return_value=True)
    def test_pack_includes_postgres_logical_dump_without_raw_volume(
        self,
        mock_postgres_configured,
        mock_docker,
        mock_daemon,
        mock_home,
        tmp_path,
        runner: CliRunner,
    ) -> None:
        fake_home = tmp_path / ".gobby"
        fake_home.mkdir()
        mock_home.return_value = fake_home

        def _backup(*, output_dir: Path, gobby_home: Path) -> dict[str, str]:
            output_dir.mkdir(parents=True)
            dump = output_dir / "gobby.dump"
            metadata = output_dir / "metadata.json"
            sums = output_dir / "SHA256SUMS"
            dump.write_bytes(b"dump")
            metadata.write_text(json.dumps({"dump_sha256": "a" * 64}), encoding="utf-8")
            sums.write_text(f"{'a' * 64}  gobby.dump\n", encoding="utf-8")
            return {"dump_path": str(dump), "backup_dir": str(output_dir), "mode": "docker"}

        with patch("gobby.cli.pack.create_postgres_backup", side_effect=_backup):
            out_path = tmp_path / "out.tar.gz"
            result = runner.invoke(pack, [str(out_path)])

        assert result.exit_code == 0
        with tarfile.open(out_path, "r:gz") as tar:
            names = tar.getnames()
            manifest = json.loads(tar.extractfile("gobby/manifest.json").read() or b"{}")
        assert "gobby/postgres/gobby.dump" in names
        assert "gobby/postgres/metadata.json" in names
        assert "gobby/postgres/SHA256SUMS" in names
        assert "gobby/docker-volumes/gobby_postgres_data.tar.gz" not in names
        assert manifest["postgres_backup"] is True
        assert "postgres_install_mode" not in manifest

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=True)
    @patch("gobby.cli.pack.stop_daemon")
    @patch("gobby.cli.pack._start_daemon")
    @patch("gobby.cli.pack._docker_available", return_value=False)
    def test_pack_daemon_lifecycle(
        self,
        mock_docker,
        mock_start,
        mock_stop,
        mock_daemon,
        mock_home,
        tmp_path,
        runner: CliRunner,
    ) -> None:
        fake_home = tmp_path / ".gobby"
        fake_home.mkdir()

        mock_home.return_value = fake_home

        out_path = tmp_path / "out.tar.gz"
        result = runner.invoke(pack, [str(out_path)])

        assert result.exit_code == 0
        mock_stop.assert_called_once()
        assert mock_stop.call_count == 1
        assert mock_stop.call_args is not None
        mock_start.assert_called_once()
        assert mock_start.call_count == 1
        assert mock_start.call_args is not None

    def test_pack_preserves_primary_error_and_completes_cleanup(
        self,
        tmp_path: Path,
        runner: CliRunner,
    ) -> None:
        fake_home = tmp_path / ".gobby"
        fake_home.mkdir()
        primary_error = RuntimeError("archive failed")
        cleanup_error = RuntimeError("docker restart failed")

        with (
            patch("gobby.cli.pack.get_gobby_home", return_value=fake_home),
            patch("gobby.cli.pack._daemon_is_running", return_value=True),
            patch("gobby.cli.pack.stop_daemon"),
            patch("gobby.cli.pack._start_daemon") as start_daemon,
            patch("gobby.cli.pack._docker_available", return_value=True),
            patch("gobby.cli.pack._volume_exists", return_value=True),
            patch("gobby.cli.pack._stop_docker_services", return_value=True),
            patch(
                "gobby.cli.pack._start_docker_services",
                side_effect=cleanup_error,
            ),
            patch("gobby.cli.pack._do_pack", side_effect=primary_error),
        ):
            result = runner.invoke(pack, [str(tmp_path / "out.tar.gz")])

        assert result.exception is primary_error
        start_daemon.assert_called_once_with()
        assert "Warning: Failed to restart Docker services: docker restart failed" in result.output


class TestUnpackCommand:
    def _create_fake_archive(self, tmp_path: Path) -> Path:
        out_path = tmp_path / "testpack.tar.gz"
        with tarfile.open(out_path, "w:gz") as tar:
            # manifest
            m_path = tmp_path / "manifest.json"
            m_path.write_text(json.dumps({"version": 1}))
            tar.add(str(m_path), arcname="gobby/manifest.json")

            # db
            db_path = tmp_path / "hub-postgres.db"
            db_path.write_text("restored db")
            tar.add(str(db_path), arcname="gobby/hub-postgres.db")

            bootstrap_path = tmp_path / "bootstrap.yaml"
            bootstrap_path.write_text("hub_backend: postgres\n")
            tar.add(str(bootstrap_path), arcname="gobby/bootstrap.yaml")

            machine_id_path = tmp_path / "machine_id"
            machine_id_path.write_text("8fa1247f-e924-4bd7-a54e-b9dd5704304a")
            tar.add(str(machine_id_path), arcname="gobby/machine_id")
        return out_path

    def _create_postgres_archive(self, tmp_path: Path) -> Path:
        out_path = tmp_path / "postgres-pack.tar.gz"
        with tarfile.open(out_path, "w:gz") as tar:
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(json.dumps({"version": 1, "postgres_backup": True}))
            tar.add(str(manifest_path), arcname="gobby/manifest.json")

            dump_path = tmp_path / "gobby.dump"
            dump_path.write_bytes(b"dump")
            tar.add(str(dump_path), arcname="gobby/postgres/gobby.dump")

            metadata_path = tmp_path / "metadata.json"
            metadata_path.write_text(json.dumps({"dump_sha256": "a" * 64}))
            tar.add(str(metadata_path), arcname="gobby/postgres/metadata.json")

            sums_path = tmp_path / "SHA256SUMS"
            sums_path.write_text(f"{'a' * 64}  gobby.dump\n")
            tar.add(str(sums_path), arcname="gobby/postgres/SHA256SUMS")
        return out_path

    @patch("gobby.cli.pack.get_gobby_home")
    def test_unpack_dry_run(
        self, mock_home: MagicMock, pack_env: PackEnv, tmp_path: Path, runner: CliRunner
    ) -> None:
        archive = self._create_fake_archive(tmp_path)
        mock_home.return_value = pack_env.home

        result = runner.invoke(unpack, [str(archive), "--dry-run"])
        assert result.exit_code == 0
        assert "Contents:" in result.output
        assert "gobby/hub-postgres.db" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    @patch("gobby.cli.pack.install_git_hooks", return_value={"success": True, "installed": []})
    def test_unpack_success(
        self,
        mock_hooks: MagicMock,
        mock_docker: MagicMock,
        mock_daemon: MagicMock,
        mock_home: MagicMock,
        pack_env: PackEnv,
        tmp_path: Path,
        runner: CliRunner,
    ) -> None:
        archive = self._create_fake_archive(tmp_path)
        mock_home.return_value = pack_env.home

        result = runner.invoke(unpack, [str(archive), "--force"])
        assert result.exit_code == 0, result.output

        assert (pack_env.home / "hub-postgres.db").read_text() == "restored db"
        dest_boot = (pack_env.home / "bootstrap.yaml").read_text()
        assert str(pack_env.files_home) in dest_boot
        assert not (pack_env.home / "machine_id").exists()
        assert "Restored: hub-postgres.db" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    @patch("gobby.cli.pack.install_git_hooks", return_value={"success": True, "installed": []})
    def test_unpack_restore_identity_is_explicit(
        self,
        mock_hooks: MagicMock,
        mock_docker: MagicMock,
        mock_daemon: MagicMock,
        mock_home: MagicMock,
        pack_env: PackEnv,
        tmp_path: Path,
        runner: CliRunner,
    ) -> None:
        archive = self._create_fake_archive(tmp_path)
        mock_home.return_value = pack_env.home

        result = runner.invoke(unpack, [str(archive), "--restore-identity", "--force"])

        assert result.exit_code == 0, result.output
        assert (pack_env.home / "machine_id").read_text() == "8fa1247f-e924-4bd7-a54e-b9dd5704304a"

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._stop_docker_services", return_value=False)
    @pytest.mark.parametrize(
        ("member_name", "expected"),
        [
            ("gobby/../../../evil", "parent-directory traversal is not allowed"),
            ("gobby//tmp/evil", "absolute paths are not allowed"),
        ],
    )
    def test_unpack_rejects_escaping_member(
        self,
        mock_stop_services,
        mock_daemon,
        mock_home: MagicMock,
        member_name: str,
        expected: str,
        pack_env: PackEnv,
        tmp_path: Path,
        runner: CliRunner,
    ) -> None:
        archive = tmp_path / "malicious.tar.gz"
        payload = b"evil"
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo(member_name)
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))

        mock_home.return_value = pack_env.home

        result = runner.invoke(unpack, [str(archive), "--force"])

        assert result.exit_code != 0
        assert expected in result.output
        assert not (tmp_path / "evil").exists()

    @pytest.mark.parametrize(
        ("member_type", "expected"),
        [
            (tarfile.SYMTYPE, "only regular files and directories are supported"),
            (tarfile.CHRTYPE, "only regular files and directories are supported"),
        ],
    )
    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._stop_docker_services", return_value=False)
    def test_unpack_rejects_special_members(
        self,
        mock_stop_services: MagicMock,
        mock_daemon: MagicMock,
        mock_home: MagicMock,
        member_type: bytes,
        expected: str,
        pack_env: PackEnv,
        tmp_path: Path,
        runner: CliRunner,
    ) -> None:
        archive = tmp_path / "malicious.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo("gobby/unsafe")
            member.type = member_type
            member.linkname = "bootstrap.yaml"
            tar.addfile(member)

        mock_home.return_value = pack_env.home

        result = runner.invoke(unpack, [str(archive), "--force"])

        assert result.exit_code != 0
        assert expected in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    def test_unpack_aborts_if_exists(
        self, mock_home: MagicMock, pack_env: PackEnv, tmp_path: Path, runner: CliRunner
    ) -> None:
        archive = self._create_fake_archive(tmp_path)
        mock_home.return_value = pack_env.home

        # Answer NO to confirmation
        result = runner.invoke(unpack, [str(archive)], input="N\n")
        assert result.exit_code == 0  # Aborted prints, then sys.exit(0)
        assert "Aborted" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    @patch("gobby.cli.pack.install_git_hooks", return_value={"success": True, "installed": []})
    def test_unpack_force(
        self,
        mock_hooks: MagicMock,
        mock_docker: MagicMock,
        mock_daemon: MagicMock,
        mock_home: MagicMock,
        pack_env: PackEnv,
        tmp_path: Path,
        runner: CliRunner,
    ) -> None:
        archive = self._create_fake_archive(tmp_path)
        mock_home.return_value = pack_env.home

        result = runner.invoke(unpack, [str(archive), "--force"])
        assert result.exit_code == 0, result.output

        dest_boot = (pack_env.home / "bootstrap.yaml").read_text()
        assert str(pack_env.files_home) in dest_boot
        assert (pack_env.home / "hub-postgres.db").read_text() == "restored db"

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._stop_docker_services", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    @patch("gobby.cli.pack.install_git_hooks", return_value={"success": True, "installed": []})
    def test_unpack_restores_postgres_payload(
        self,
        mock_hooks,
        mock_docker,
        mock_stop_services,
        mock_daemon,
        mock_home: MagicMock,
        pack_env: PackEnv,
        tmp_path: Path,
        runner: CliRunner,
    ) -> None:
        archive = self._create_postgres_archive(tmp_path)
        mock_home.return_value = pack_env.home
        calls: list[Path] = []

        def _restore(source: Path, **_kwargs: object) -> dict[str, object]:
            calls.append(source)
            assert (source / "gobby.dump").read_bytes() == b"dump"
            return {"verified": True}

        with (
            patch("gobby.cli.pack.restore_postgres_backup", side_effect=_restore),
            patch("gobby.cli.pack._start_docker_services") as start_services,
        ):
            result = runner.invoke(unpack, [str(archive), "--force"])

        assert result.exit_code == 0
        assert len(calls) == 1
        start_services.assert_called_once()
        assert "Restored PostgreSQL logical dump" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._stop_docker_services", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    @patch("gobby.cli.pack.install_git_hooks", return_value={"success": True, "installed": []})
    def test_unpack_no_postgres_skips_postgres_payload(
        self,
        mock_hooks: MagicMock,
        mock_docker: MagicMock,
        mock_stop_services: MagicMock,
        mock_daemon: MagicMock,
        mock_home: MagicMock,
        pack_env: PackEnv,
        tmp_path: Path,
        runner: CliRunner,
    ) -> None:
        archive = self._create_postgres_archive(tmp_path)
        mock_home.return_value = pack_env.home

        with patch("gobby.cli.pack.restore_postgres_backup") as restore:
            result = runner.invoke(unpack, [str(archive), "--force", "--no-postgres"])

        assert result.exit_code == 0
        restore.assert_not_called()
        assert "Skipped PostgreSQL restore" in result.output


@dataclass(frozen=True)
class PackEnv:
    home: Path
    files_home: Path


@pytest.fixture
def pack_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PackEnv:
    home = tmp_path / ".gobby"
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
    return PackEnv(home=home, files_home=files_home)


def _seed_files_home(files_home: Path) -> None:
    (files_home / "USER.md").write_text("profile", encoding="utf-8")
    dest = files_home / "_personal" / "attachments" / "p1"
    dest.mkdir(parents=True)
    (dest / "a.bin").write_bytes(b"att")
    wiki = files_home / "wiki" / "alpha"
    wiki.mkdir(parents=True)
    (wiki / "note.md").write_text("wiki", encoding="utf-8")


class TestFilesHomePack:
    def test_6_2_1_archives_files_home_not_personal(
        self, pack_env: PackEnv, tmp_path: Path, runner: CliRunner
    ) -> None:
        from gobby.cli.pack import PACK_FILES

        assert "personal" not in PACK_FILES
        assert "personal/USER.md" not in PACK_FILES
        _seed_files_home(pack_env.files_home)
        (pack_env.home / "personal").mkdir()
        (pack_env.home / "personal" / "USER.md").write_text("legacy", encoding="utf-8")
        out_path = tmp_path / "out.tar.gz"
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code == 0, result.output
        with tarfile.open(out_path, "r:gz") as tar:
            names = tar.getnames()
        assert "gobby/files/USER.md" in names
        assert "gobby/files/_personal/attachments/p1/a.bin" in names
        assert "gobby/files/wiki/alpha/note.md" in names
        assert not any(name.startswith("gobby/personal") for name in names)

    def test_6_2_4_dry_run_includes_files_bind(self, pack_env: PackEnv, runner: CliRunner) -> None:
        _seed_files_home(pack_env.files_home)
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
        ):
            result = runner.invoke(pack, ["--dry-run"])
        assert result.exit_code == 0, result.output
        assert "gobby/files/" in result.output
        assert "gobby/personal" not in result.output

    def test_6_2_12_refuses_symlink_and_specials(
        self, pack_env: PackEnv, tmp_path: Path, runner: CliRunner
    ) -> None:
        (pack_env.files_home / "USER.md").write_text("ok", encoding="utf-8")
        (pack_env.files_home / "link").symlink_to(pack_env.files_home / "USER.md")
        out_path = tmp_path / "out.tar.gz"
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code != 0
        assert not out_path.exists()
        (pack_env.files_home / "link").unlink()
        os.mkfifo(pack_env.files_home / "fifo")
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code != 0
        assert not out_path.exists()

    def test_6_2_13_and_6_2_16_claim_blocks_daemon_start(
        self, pack_env: PackEnv, tmp_path: Path, runner: CliRunner
    ) -> None:
        from gobby.cli.hub_backup.files_home import FilesHomeArchiveHooks
        from gobby.runner_pid_file import claim_pid_file

        _seed_files_home(pack_env.files_home)
        seen: list[bool] = []

        def _on_claimed(claim: object) -> None:
            del claim
            blocked = claim_pid_file(pack_env.home / "gobby.pid", role="daemon")
            seen.append(blocked is None)
            if blocked is not None:
                blocked.release()

        out_path = tmp_path / "out.tar.gz"
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
            patch(
                "gobby.cli.hub_backup.files_home._active_hooks",
                FilesHomeArchiveHooks(on_claimed=_on_claimed),
            ),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code == 0, result.output
        assert seen == [True]

        archive = tmp_path / "in.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            payload = b"p"
            info = tarfile.TarInfo("gobby/files/USER.md")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        seen.clear()
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
            patch(
                "gobby.cli.pack.install_git_hooks", return_value={"success": True, "installed": []}
            ),
            patch(
                "gobby.cli.hub_backup.files_home._active_hooks",
                FilesHomeArchiveHooks(on_claimed=_on_claimed),
            ),
        ):
            result = runner.invoke(unpack, [str(archive), "--force"])
        assert result.exit_code == 0, result.output
        assert seen == [True]

    def test_6_2_14_refuses_hardlink(
        self, pack_env: PackEnv, tmp_path: Path, runner: CliRunner
    ) -> None:
        src = pack_env.files_home / "USER.md"
        src.write_text("x", encoding="utf-8")
        os.link(src, pack_env.files_home / "hard")
        out_path = tmp_path / "out.tar.gz"
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code != 0
        assert not out_path.exists()

    def test_6_2_19_and_6_2_21_prewalk_limits_against_constants(self) -> None:
        from gobby.cli.hub_backup.files_home import (
            MAX_ARCHIVE_BYTES,
            MAX_ARCHIVE_MEMBERS,
            FilesHomeArchiveError,
            WalkEntry,
            preflight_archive_graph,
        )

        preflight_archive_graph(
            [WalkEntry(rel=str(i), is_dir=False, size=0) for i in range(MAX_ARCHIVE_MEMBERS)]
        )
        preflight_archive_graph([WalkEntry(rel="big", is_dir=False, size=MAX_ARCHIVE_BYTES)])
        with pytest.raises(FilesHomeArchiveError):
            preflight_archive_graph(
                [
                    WalkEntry(rel=str(i), is_dir=False, size=0)
                    for i in range(MAX_ARCHIVE_MEMBERS + 1)
                ]
            )
        with pytest.raises(FilesHomeArchiveError):
            preflight_archive_graph(
                [WalkEntry(rel="big", is_dir=False, size=MAX_ARCHIVE_BYTES + 1)]
            )

    def test_6_2_21_producer_refuses_before_output(
        self, pack_env: PackEnv, tmp_path: Path, runner: CliRunner
    ) -> None:
        from gobby.cli.hub_backup.files_home import FilesHomeArchiveHooks

        _seed_files_home(pack_env.files_home)
        out_path = tmp_path / "out.tar.gz"
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
            patch(
                "gobby.cli.hub_backup.files_home._active_hooks",
                FilesHomeArchiveHooks(force_member_count=100_001),
            ),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code != 0
        assert not out_path.exists()

    def test_6_2_22_refuses_output_inside_files_home(
        self, pack_env: PackEnv, runner: CliRunner
    ) -> None:
        _seed_files_home(pack_env.files_home)
        out_path = pack_env.files_home / "out.tar.gz"
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code != 0
        assert not out_path.exists()
        assert "files_home" in result.output.lower() or "source" in result.output.lower()

    def test_6_2_24_injected_pack_failure_preserves_prior(
        self, pack_env: PackEnv, tmp_path: Path, runner: CliRunner
    ) -> None:
        from gobby.cli.hub_backup.files_home import FilesHomeArchiveHooks

        _seed_files_home(pack_env.files_home)
        out_path = tmp_path / "out.tar.gz"
        out_path.write_bytes(b"prior-archive")
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
            patch(
                "gobby.cli.hub_backup.files_home._active_hooks",
                FilesHomeArchiveHooks(fail_temp_write=True),
            ),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code != 0
        assert out_path.read_bytes() == b"prior-archive"
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code == 0, result.output
        assert out_path.stat().st_size > 0

    def test_6_2_25_swap_after_prewalk_leaves_no_archive(
        self, pack_env: PackEnv, tmp_path: Path, runner: CliRunner
    ) -> None:
        from gobby.cli.hub_backup.files_home import FilesHomeArchiveHooks

        _seed_files_home(pack_env.files_home)
        target = pack_env.files_home / "USER.md"

        def _swap(_entries: object) -> None:
            target.unlink()
            target.write_text("swapped", encoding="utf-8")

        out_path = tmp_path / "out.tar.gz"
        with (
            patch("gobby.cli.pack._daemon_is_running", return_value=False),
            patch("gobby.cli.pack._docker_available", return_value=False),
            patch(
                "gobby.cli.hub_backup.files_home._active_hooks",
                FilesHomeArchiveHooks(after_prewalk=_swap),
            ),
        ):
            result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code != 0
        assert not out_path.exists() or out_path.stat().st_size == 0
        leftover = list(tmp_path.glob(".out.tar.gz.*.tmp")) + list(tmp_path.glob("*.tmp"))
        assert leftover == []
