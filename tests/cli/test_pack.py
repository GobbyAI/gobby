"""Tests for gobby pack and unpack CLI commands."""

import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.pack import _human_size, pack, unpack

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
        (fake_home / "gobby-hub.db").write_text("fake db")
        (fake_home / "session_transcripts").mkdir()
        (fake_home / "session_transcripts" / "1.txt").write_text("ts")

        mock_home.return_value = fake_home

        result = runner.invoke(pack, ["--dry-run"])
        assert result.exit_code == 0
        assert "Pack contents (dry run):" in result.output
        assert "gobby/gobby-hub.db" in result.output
        assert "gobby/session_transcripts/" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    def test_pack_success(
        self, mock_docker, mock_daemon, mock_home, tmp_path, runner: CliRunner
    ) -> None:
        fake_home = tmp_path / ".gobby"
        fake_home.mkdir()
        (fake_home / "gobby-hub.db").write_text("db content")

        mock_home.return_value = fake_home

        out_path = tmp_path / "out.tar.gz"
        result = runner.invoke(pack, [str(out_path)])
        assert result.exit_code == 0
        assert "Packing Gobby data" in result.output
        assert out_path.exists()

        # Verify tarball
        with tarfile.open(out_path, "r:gz") as tar:
            names = tar.getnames()
            assert "gobby/manifest.json" in names
            assert "gobby/gobby-hub.db" in names

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
            return {"dump_path": str(dump), "backup_dir": str(output_dir)}

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


class TestUnpackCommand:
    def _create_fake_archive(self, tmp_path: Path) -> Path:
        out_path = tmp_path / "testpack.tar.gz"
        with tarfile.open(out_path, "w:gz") as tar:
            # manifest
            m_path = tmp_path / "manifest.json"
            m_path.write_text(json.dumps({"version": 1}))
            tar.add(str(m_path), arcname="gobby/manifest.json")

            # db
            db_path = tmp_path / "gobby-hub.db"
            db_path.write_text("restored db")
            tar.add(str(db_path), arcname="gobby/gobby-hub.db")
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
    def test_unpack_dry_run(self, mock_home, tmp_path, runner: CliRunner) -> None:
        archive = self._create_fake_archive(tmp_path)
        mock_home.return_value = tmp_path / ".gobby"

        result = runner.invoke(unpack, [str(archive), "--dry-run"])
        assert result.exit_code == 0
        assert "Contents:" in result.output
        assert "gobby/gobby-hub.db" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    @patch("gobby.cli.pack.install_git_hooks", return_value={"success": True, "installed": []})
    def test_unpack_success(
        self, mock_hooks, mock_docker, mock_daemon, mock_home, tmp_path, runner: CliRunner
    ) -> None:
        archive = self._create_fake_archive(tmp_path)

        fake_home = tmp_path / ".gobby"
        # don't exist yet to avoid safety check block
        mock_home.return_value = fake_home

        result = runner.invoke(unpack, [str(archive)])
        assert result.exit_code == 0

        assert (fake_home / "gobby-hub.db").exists()
        assert (fake_home / "gobby-hub.db").read_text() == "restored db"

    @patch("gobby.cli.pack.get_gobby_home")
    def test_unpack_aborts_if_exists(self, mock_home, tmp_path, runner: CliRunner) -> None:
        archive = self._create_fake_archive(tmp_path)

        fake_home = tmp_path / ".gobby"
        fake_home.mkdir()
        (fake_home / "gobby-hub.db").write_text("existing")
        mock_home.return_value = fake_home

        # Answer NO to confirmation
        result = runner.invoke(unpack, [str(archive)], input="N\n")
        assert result.exit_code == 0  # Aborted prints, then sys.exit(0)
        assert "Aborted" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    @patch("gobby.cli.pack.install_git_hooks", return_value={"success": True, "installed": []})
    def test_unpack_force(
        self, mock_hooks, mock_docker, mock_daemon, mock_home, tmp_path, runner: CliRunner
    ) -> None:
        archive = self._create_fake_archive(tmp_path)

        fake_home = tmp_path / ".gobby"
        fake_home.mkdir()
        (fake_home / "gobby-hub.db").write_text("old")

        mock_home.return_value = fake_home

        # Note we pass --force to bypass confirmation
        result = runner.invoke(unpack, [str(archive), "--force"])
        assert result.exit_code == 0

        assert (fake_home / "gobby-hub.db").read_text() == "restored db"

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
        mock_home,
        tmp_path,
        runner: CliRunner,
    ) -> None:
        archive = self._create_postgres_archive(tmp_path)
        fake_home = tmp_path / ".gobby"
        mock_home.return_value = fake_home
        calls: list[Path] = []

        def _restore(source: Path, **_kwargs: object) -> dict[str, object]:
            calls.append(source)
            assert (source / "gobby.dump").read_bytes() == b"dump"
            return {"verified": True}

        with patch("gobby.cli.pack.restore_postgres_backup", side_effect=_restore):
            result = runner.invoke(unpack, [str(archive), "--force"])

        assert result.exit_code == 0
        assert len(calls) == 1
        assert "Restored PostgreSQL logical dump" in result.output

    @patch("gobby.cli.pack.get_gobby_home")
    @patch("gobby.cli.pack._daemon_is_running", return_value=False)
    @patch("gobby.cli.pack._stop_docker_services", return_value=False)
    @patch("gobby.cli.pack._docker_available", return_value=False)
    @patch("gobby.cli.pack.install_git_hooks", return_value={"success": True, "installed": []})
    def test_unpack_no_postgres_skips_postgres_payload(
        self,
        mock_hooks,
        mock_docker,
        mock_stop_services,
        mock_daemon,
        mock_home,
        tmp_path,
        runner: CliRunner,
    ) -> None:
        archive = self._create_postgres_archive(tmp_path)
        mock_home.return_value = tmp_path / ".gobby"

        with patch("gobby.cli.pack.restore_postgres_backup") as restore:
            result = runner.invoke(unpack, [str(archive), "--force", "--no-postgres"])

        assert result.exit_code == 0
        restore.assert_not_called()
        assert "Skipped PostgreSQL restore" in result.output
