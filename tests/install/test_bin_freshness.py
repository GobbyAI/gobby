from __future__ import annotations

import io
import json
import os
import tarfile
from http.client import IncompleteRead
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.install.bin_freshness_github import (
    GithubAPIError,
    GithubReleaseClient,
    SourceUnavailableError,
)
from gobby.install.bin_freshness_inspector import inspect_managed_bin
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_freshness_models import ManagedBinSpec, ReleaseAsset, managed_bin_specs
from gobby.install.bin_freshness_updater import update_all_managed_bins, update_managed_bin
from gobby.storage.bin_update_state import BinUpdateStateStore
from gobby.storage.hub.protocol import HubDatabase
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


def _spec(name: str = "ghook", floor: str = "0.4.1") -> ManagedBinSpec:
    return ManagedBinSpec(
        name=name,
        floor_version=floor,
        tag_prefix=f"{name}-v",
        artifact_name=name,
        stamp_name=f".{name}-version",
        sidecar_name=f".{name}-install.json",
    )


def _db(tmp_path: Path) -> HubDatabase:
    db = HubDatabase(tmp_path / "bin-state.db")
    run_migrations(db)
    return db


def _write_binary(bin_dir: Path, spec: ManagedBinSpec, content: bytes = b"old") -> Path:
    path = bin_dir / spec.binary_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def _write_stamp(bin_dir: Path, spec: ManagedBinSpec, version: str) -> None:
    (bin_dir / spec.stamp_name).write_text(f"{version}\n", encoding="utf-8")


def _tar_with_binary(spec: ManagedBinSpec, payload: bytes = b"new") -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        info = tarfile.TarInfo(f"{spec.name}/{spec.binary_name}")
        info.mode = 0o755
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return data.getvalue()


class FakeClient:
    def __init__(
        self,
        *,
        asset: ReleaseAsset | None = None,
        archive: bytes | None = None,
        resolve_error: Exception | None = None,
        download_error: Exception | None = None,
    ) -> None:
        self.asset = asset
        self.archive = archive or b""
        self.resolve_error = resolve_error
        self.download_error = download_error
        self.downloads = 0

    def resolve_latest_asset(self, spec: ManagedBinSpec, *, target: str) -> ReleaseAsset:
        if self.resolve_error is not None:
            raise self.resolve_error
        assert self.asset is not None
        return self.asset

    def download_asset(self, asset: ReleaseAsset) -> bytes:
        self.downloads += 1
        if self.download_error is not None:
            raise self.download_error
        return self.archive


class _IncompleteReadResponse:
    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        raise IncompleteRead(b"partial", 1)


def _asset(spec: ManagedBinSpec, version: str = "0.4.1") -> ReleaseAsset:
    return ReleaseAsset(
        tag_name=f"{spec.tag_prefix}{version}",
        version=version,
        asset_name=f"{spec.name}-aarch64-apple-darwin.tar.gz",
        asset_url=f"https://example.invalid/{spec.name}.tar.gz",
        target="aarch64-apple-darwin",
    )


class TestBinInspector:
    def test_current_binary_reads_stamp_and_sidecar(self, tmp_path: Path) -> None:
        spec = _spec()
        _write_binary(tmp_path, spec)
        _write_stamp(tmp_path, spec, "0.4.1")
        (tmp_path / spec.sidecar_name).write_text(
            json.dumps({"installed_at": "2026-05-04T00:00:00+00:00"}),
            encoding="utf-8",
        )

        inspection = inspect_managed_bin(spec, bin_dir=tmp_path)

        assert inspection.binary_exists is True
        assert inspection.installed_version == "0.4.1"
        assert inspection.installed_at == "2026-05-04T00:00:00+00:00"
        assert inspection.floor_drift is False

    def test_stale_stamp_marks_floor_drift(self, tmp_path: Path) -> None:
        spec = _spec()
        _write_binary(tmp_path, spec)
        _write_stamp(tmp_path, spec, "0.4.0")

        assert inspect_managed_bin(spec, bin_dir=tmp_path).floor_drift is True

    def test_missing_binary(self, tmp_path: Path) -> None:
        inspection = inspect_managed_bin(_spec(), bin_dir=tmp_path)

        assert inspection.binary_exists is False
        assert inspection.installed_version is None
        assert inspection.floor_drift is True

    def test_missing_stamp_uses_binary_mtime(self, tmp_path: Path) -> None:
        spec = _spec()
        _write_binary(tmp_path, spec)

        inspection = inspect_managed_bin(spec, bin_dir=tmp_path)

        assert inspection.installed_version is None
        assert inspection.installed_at is not None
        assert inspection.floor_drift is True

    def test_symlink_is_dev_state(self, tmp_path: Path) -> None:
        spec = _spec()
        target = tmp_path / "dev-ghook"
        target.write_text("dev", encoding="utf-8")
        target.chmod(0o755)
        (tmp_path / spec.binary_name).symlink_to(target)
        _write_stamp(tmp_path, spec, "0.4.1")

        inspection = inspect_managed_bin(spec, bin_dir=tmp_path)

        assert inspection.binary_exists is True
        assert inspection.is_dev is True

    def test_corrupt_sidecar_is_reported_and_mtime_fallback_is_used(self, tmp_path: Path) -> None:
        spec = _spec()
        _write_binary(tmp_path, spec)
        _write_stamp(tmp_path, spec, "0.4.1")
        (tmp_path / spec.sidecar_name).write_text("{", encoding="utf-8")

        inspection = inspect_managed_bin(spec, bin_dir=tmp_path)

        assert inspection.sidecar_error is not None
        assert inspection.installed_at is not None


class TestBinUpdater:
    def test_github_up_to_date_records_without_downloading(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec)
        _write_stamp(bin_dir, spec, "0.4.1")
        client = FakeClient(asset=_asset(spec))

        record = update_managed_bin(
            db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert record is not None
        assert record.last_status == "up_to_date"
        assert client.downloads == 0

    def test_github_newer_installed_version_records_without_downloading(
        self,
        tmp_path: Path,
    ) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec)
        _write_stamp(bin_dir, spec, "0.4.2")
        client = FakeClient(asset=_asset(spec, "0.4.1"))

        record = update_managed_bin(
            db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert record is not None
        assert record.last_status == "up_to_date"
        assert record.installed_version == "0.4.2"
        assert client.downloads == 0

    def test_staged_github_upgrade_promotes_binary_stamp_and_sidecar(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec, b"old")
        _write_stamp(bin_dir, spec, "0.4.0")
        asset = _asset(spec, "0.4.1")
        client = FakeClient(asset=asset, archive=_tar_with_binary(spec, b"new"))

        record = update_managed_bin(
            db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert record is not None
        assert record.last_status == "updated"
        assert (bin_dir / spec.binary_name).read_bytes() == b"new"
        assert (bin_dir / spec.stamp_name).read_text(encoding="utf-8").strip() == "0.4.1"
        sidecar = json.loads((bin_dir / spec.sidecar_name).read_text(encoding="utf-8"))
        assert sidecar["install_method"] == "github-release"
        assert sidecar["installed_version"] == "0.4.1"

    def test_github_api_failure_records_failed_and_keeps_binary(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec, b"old")
        _write_stamp(bin_dir, spec, "0.4.1")
        client = FakeClient(resolve_error=GithubAPIError("api down"))

        record = update_managed_bin(
            db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert record is not None
        assert record.last_status == "failed"
        assert (bin_dir / spec.binary_name).read_bytes() == b"old"

    def test_missing_release_tag_records_source_unavailable_at_floor(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec)
        _write_stamp(bin_dir, spec, "0.4.1")
        client = FakeClient(resolve_error=SourceUnavailableError("missing release tag"))

        record = update_managed_bin(
            db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert record is not None
        assert record.last_status == "source_unavailable"
        assert "missing release tag" in (record.last_error or "")

    def test_missing_platform_asset_records_source_unavailable_at_floor(
        self, tmp_path: Path
    ) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec)
        _write_stamp(bin_dir, spec, "0.4.1")
        client = FakeClient(resolve_error=SourceUnavailableError("missing platform asset"))

        record = update_managed_bin(
            db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert record is not None
        assert record.last_status == "source_unavailable"
        assert "missing platform asset" in (record.last_error or "")

    def test_atomic_promotion_failure_keeps_existing_binary(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec, b"old")
        _write_stamp(bin_dir, spec, "0.4.1")
        client = FakeClient(asset=_asset(spec, "0.4.2"), archive=_tar_with_binary(spec, b"new"))

        original_replace = os.replace

        def fail_final_replace(src: str | Path, dst: str | Path) -> None:
            if Path(dst) == bin_dir / spec.binary_name:
                raise OSError("replace failed")
            original_replace(src, dst)

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "gobby.install.bin_freshness_updater.os.replace", fail_final_replace
            )
            record = update_managed_bin(
                db,
                spec,
                BinFreshnessConfig(),
                bin_dir=bin_dir,
                client=client,
            )

        assert record is not None
        assert record.last_status == "failed"
        assert (bin_dir / spec.binary_name).read_bytes() == b"old"

    def test_lock_held_skip_leaves_existing_state(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True)
        spec = _spec()
        store = BinUpdateStateStore(db)
        store.upsert(
            tool_name=spec.name,
            installed_version="0.4.1",
            floor_version=spec.floor_version,
            latest_version="0.4.1",
            binary_path=None,
            target="aarch64-apple-darwin",
            last_status="up_to_date",
            last_error=None,
            installed_at=None,
            source_url=None,
            is_dev=False,
            floor_drift=False,
        )

        lock = try_acquire_native_bin_lock(spec.name, bin_dir=bin_dir)
        assert lock is not None
        try:
            result = update_managed_bin(
                db,
                spec,
                BinFreshnessConfig(),
                bin_dir=bin_dir,
                client=FakeClient(resolve_error=RuntimeError("should not run")),
            )
        finally:
            lock.release()

        assert result is None
        assert store.get(spec.name).last_status == "up_to_date"

    def test_source_unavailable_below_floor_records_floor_violation(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec)
        _write_stamp(bin_dir, spec, "0.4.0")
        client = FakeClient(resolve_error=SourceUnavailableError("missing asset"))

        record = update_managed_bin(
            db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert record is not None
        assert record.last_status == "floor_violated"

    def test_update_all_contains_internal_errors(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        bin_dir = tmp_path / "bin"
        client = FakeClient(resolve_error=RuntimeError("boom"))

        records = update_all_managed_bins(
            db,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert len(records) == len(managed_bin_specs())
        assert {record.last_status for record in records} == {"failed"}


class TestGithubReleaseClient:
    def test_fetch_releases_wraps_incomplete_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "gobby.install.bin_freshness_github._urlopen_https",
            lambda _req, **_kwargs: _IncompleteReadResponse(),
        )
        client = GithubReleaseClient(timeout_seconds=1)

        with pytest.raises(GithubAPIError) as exc_info:
            client.fetch_releases()

        assert isinstance(exc_info.value.__cause__, IncompleteRead)

    def test_download_asset_wraps_incomplete_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "gobby.install.bin_freshness_github._urlopen_https",
            lambda _req, **_kwargs: _IncompleteReadResponse(),
        )
        client = GithubReleaseClient(timeout_seconds=1)

        with pytest.raises(GithubAPIError) as exc_info:
            client.download_asset(_asset(_spec()))

        assert isinstance(exc_info.value.__cause__, IncompleteRead)
