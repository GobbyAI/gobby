from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from http.client import IncompleteRead
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest

import gobby.install.bin_freshness_locks as freshness_locks
from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.install.bin_freshness_github import (
    GithubAPIError,
    GithubReleaseClient,
    SourceUnavailableError,
)
from gobby.install.bin_freshness_inspector import inspect_managed_bin
from gobby.install.bin_freshness_locks import NativeBinFileLock, try_acquire_native_bin_lock
from gobby.install.bin_freshness_models import ManagedBinSpec, ReleaseAsset, managed_bin_specs
from gobby.install.bin_freshness_updater import update_all_managed_bins, update_managed_bin
from gobby.storage.bin_update_state import BinUpdateStateStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


def test_lock_close_error_does_not_mask_unlock_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlock_error = OSError("unlock failed")

    class FailingFcntl:
        LOCK_UN = 8

        @staticmethod
        def flock(_fd: int, _operation: int) -> None:
            raise unlock_error

    def fail_close(_fd: int) -> None:
        raise OSError("close failed")

    # freshness_locks.os is the os module: scope the close patch to release() so
    # fixture teardown (temp-dir cleanup) sees the real os.close.
    with monkeypatch.context() as patched:
        patched.setattr(freshness_locks, "_fcntl", FailingFcntl())
        patched.setattr(freshness_locks.os, "close", fail_close)
        lock = NativeBinFileLock(tmp_path / "lock", 123)

        with pytest.raises(OSError, match="unlock failed") as exc_info:
            lock.release()

    assert exc_info.value is unlock_error


def _spec(name: str = "ghook", floor: str = "0.4.1") -> ManagedBinSpec:
    return ManagedBinSpec(
        name=name,
        floor_version=floor,
        tag_prefix=f"{name}-v",
        artifact_name=name,
        stamp_name=f".{name}-version",
        sidecar_name=f".{name}-install.json",
    )


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

    def read(self, _size: int = -1) -> bytes:
        raise IncompleteRead(b"partial", 1)


class _JsonResponse:
    def __init__(self, payload: object, *, link: str | None = None) -> None:
        self.payload = payload
        self.headers = {"Link": link} if link is not None else {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        payload = json.dumps(self.payload).encode("utf-8")
        return payload if size < 0 else payload[:size]


class _BytesResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.read_sizes: list[int] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.payload if size < 0 else self.payload[:size]


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

    def test_current_binary_prefers_probed_version_over_stale_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _spec()
        _write_binary(tmp_path, spec)
        _write_stamp(tmp_path, spec, "0.4.0")
        monkeypatch.setattr(
            "gobby.install.bin_freshness_inspector._probe_binary_version",
            lambda _path: "0.4.1",
        )

        inspection = inspect_managed_bin(spec, bin_dir=tmp_path)

        assert inspection.installed_version == "0.4.1"
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
    def test_floor_satisfied_binary_records_latest_release_without_download(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec)
        _write_stamp(bin_dir, spec, "0.4.1")
        asset = _asset(spec, "0.4.3")
        client = FakeClient(asset=asset)

        record = update_managed_bin(
            db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert record is not None
        assert record.last_status == "up_to_date"
        assert record.latest_version == "0.4.3"
        assert record.source_url == asset.asset_url
        assert client.downloads == 0

    def test_github_up_to_date_records_without_downloading(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
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
        assert record.latest_version == "0.4.1"
        assert record.source_url == client.asset.asset_url
        assert client.downloads == 0

    def test_floor_ahead_of_release_does_not_redownload_current_binary(
        self,
        tmp_path: Path,
        postgres_db: HubDatabase,
    ) -> None:
        db = postgres_db
        bin_dir = tmp_path / "bin"
        spec = _spec(floor="0.4.3")
        binary = _write_binary(bin_dir, spec)
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
        assert record.latest_version == "0.4.1"
        assert client.downloads == 0
        assert binary.read_bytes() == b"old"

    def test_staged_github_upgrade_promotes_binary_stamp_and_sidecar(
        self,
        tmp_path: Path,
        postgres_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = postgres_db
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec, b"old")
        _write_stamp(bin_dir, spec, "0.4.0")
        asset = _asset(spec, "0.4.1")
        client = FakeClient(asset=asset, archive=_tar_with_binary(spec, b"new"))
        events: list[str] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def tracked_fsync(fd: int) -> None:
            events.append("fsync")
            real_fsync(fd)

        def tracked_replace(src: str | Path, dst: str | Path) -> None:
            events.append(f"replace:{Path(dst).name}")
            real_replace(src, dst)

        monkeypatch.setattr("gobby.install.bin_freshness_promotion.os.fsync", tracked_fsync)
        monkeypatch.setattr("gobby.install.bin_freshness_promotion.os.replace", tracked_replace)

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
        assert events.index("fsync") < events.index(f"replace:{spec.binary_name}")
        assert (bin_dir / spec.stamp_name).read_text(encoding="utf-8").strip() == "0.4.1"
        sidecar = json.loads((bin_dir / spec.sidecar_name).read_text(encoding="utf-8"))
        assert sidecar["install_method"] == "github-release"
        assert sidecar["installed_version"] == "0.4.1"

    def test_github_api_failure_records_failed_and_keeps_binary(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec, b"old")
        _write_stamp(bin_dir, spec, "0.4.0")
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

    def test_checksum_failure_records_failed_and_keeps_binary(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec, b"old")
        _write_stamp(bin_dir, spec, "0.4.0")
        client = FakeClient(
            asset=_asset(spec, "0.4.1"),
            download_error=GithubAPIError("checksum mismatch"),
        )

        record = update_managed_bin(
            postgres_db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=client,
        )

        assert record is not None
        assert record.last_status == "failed"
        assert record.last_error == "checksum mismatch"
        assert (bin_dir / spec.binary_name).read_bytes() == b"old"

    def test_missing_release_tag_records_error_when_installed_at_floor(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
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
        assert record.last_status == "up_to_date"
        assert record.latest_version is None
        assert record.last_error == "missing release tag"

    def test_missing_platform_asset_records_error_when_installed_at_floor(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
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
        assert record.last_status == "up_to_date"
        assert record.latest_version is None
        assert record.last_error == "missing platform asset"

    def test_github_api_failure_records_error_when_installed_at_floor(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec)
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
        assert record.last_status == "up_to_date"
        assert record.latest_version is None
        assert record.last_error == "api down"

    def test_atomic_promotion_failure_keeps_existing_binary(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
        bin_dir = tmp_path / "bin"
        spec = _spec()
        _write_binary(bin_dir, spec, b"old")
        _write_stamp(bin_dir, spec, "0.4.0")
        client = FakeClient(asset=_asset(spec, "0.4.2"), archive=_tar_with_binary(spec, b"new"))

        original_replace = os.replace

        def fail_final_replace(src: str | Path, dst: str | Path) -> None:
            if Path(dst) == bin_dir / spec.binary_name:
                raise OSError("replace failed")
            original_replace(src, dst)

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "gobby.install.bin_freshness_promotion.os.replace", fail_final_replace
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

    def test_lock_held_skip_leaves_existing_state(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True)
        spec = _spec()
        store = BinUpdateStateStore(db, machine_id=TEST_MACHINE_ID)
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

    def test_source_unavailable_below_floor_records_floor_violation(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
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

    def test_update_all_contains_internal_errors(
        self, tmp_path: Path, postgres_db: HubDatabase
    ) -> None:
        db = postgres_db
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

    def test_download_asset_verifies_checksum_and_caps_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = b"release archive"
        digest = hashlib.sha256(payload).hexdigest()
        checksum_response = _BytesResponse(f"{digest}  archive.tar.gz\n".encode())
        asset_response = _BytesResponse(payload)
        responses = iter([checksum_response, asset_response])
        calls: list[str] = []

        def fake_urlopen(req: Any, **_kwargs: Any) -> _BytesResponse:
            calls.append(req.full_url)
            return next(responses)

        monkeypatch.setattr("gobby.install.bin_freshness_github._urlopen_https", fake_urlopen)
        client = GithubReleaseClient(timeout_seconds=1)
        asset = _asset(_spec())

        assert client.download_asset(asset) == payload
        assert calls == [f"{asset.asset_url}.sha256", asset.asset_url]
        assert checksum_response.read_sizes == [16 * 1024 + 1]
        assert asset_response.read_sizes == [128 * 1024 * 1024 + 1]

    def test_download_asset_rejects_checksum_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        responses = iter([_BytesResponse(("0" * 64).encode()), _BytesResponse(b"tampered")])
        monkeypatch.setattr(
            "gobby.install.bin_freshness_github._urlopen_https",
            lambda _req, **_kwargs: next(responses),
        )
        client = GithubReleaseClient(timeout_seconds=1)

        with pytest.raises(GithubAPIError, match="checksum mismatch"):
            client.download_asset(_asset(_spec()))

    def test_download_asset_rejects_invalid_checksum_without_fetching_asset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def fake_urlopen(req: Any, **_kwargs: Any) -> _BytesResponse:
            calls.append(req.full_url)
            return _BytesResponse(b"invalid")

        monkeypatch.setattr("gobby.install.bin_freshness_github._urlopen_https", fake_urlopen)
        client = GithubReleaseClient(timeout_seconds=1)
        asset = _asset(_spec())

        with pytest.raises(GithubAPIError, match="invalid SHA-256 checksum"):
            client.download_asset(asset)

        assert calls == [f"{asset.asset_url}.sha256"]

    def test_download_asset_rejects_oversized_asset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b"12345"
        digest = hashlib.sha256(payload).hexdigest()
        responses = iter([_BytesResponse(digest.encode()), _BytesResponse(payload)])
        monkeypatch.setattr(
            "gobby.install.bin_freshness_github._urlopen_https",
            lambda _req, **_kwargs: next(responses),
        )
        monkeypatch.setattr("gobby.install.bin_freshness_github._MAX_RELEASE_ASSET_BYTES", 4)
        client = GithubReleaseClient(timeout_seconds=1)

        with pytest.raises(GithubAPIError, match="4-byte download limit"):
            client.download_asset(_asset(_spec()))

    def test_resolve_latest_asset_resolves_from_canonical_repo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _spec()
        target = "aarch64-apple-darwin"
        expected_asset = f"{spec.name}-{target}.tar.gz"
        calls: list[str] = []

        def fake_urlopen(req: Any, **_kwargs: Any) -> _JsonResponse:
            calls.append(req.full_url)
            return _JsonResponse(
                [
                    {
                        "tag_name": "ghook-v0.4.3",
                        "draft": False,
                        "prerelease": False,
                        "published_at": "2026-07-01T00:00:00Z",
                        "assets": [
                            {
                                "name": expected_asset,
                                "browser_download_url": (
                                    "https://github.com/GobbyAI/gobby/releases/download/"
                                    f"ghook-v0.4.3/{expected_asset}"
                                ),
                            }
                        ],
                    }
                ]
            )

        monkeypatch.setattr("gobby.install.bin_freshness_github._urlopen_https", fake_urlopen)
        client = GithubReleaseClient(timeout_seconds=1)

        asset = client.resolve_latest_asset(spec, target=target)

        assert asset.tag_name == "ghook-v0.4.3"
        assert asset.asset_name == expected_asset
        assert asset.asset_url.startswith("https://github.com/GobbyAI/gobby/")
        assert calls == [
            "https://api.github.com/repos/GobbyAI/gobby/releases?per_page=100",
        ]

    def test_floor_recovery_download_finds_release_beyond_first_page(
        self,
        tmp_path: Path,
        postgres_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec = _spec()
        target = "aarch64-apple-darwin"
        expected_asset = f"{spec.name}-{target}.tar.gz"
        asset_url = (
            "https://github.com/GobbyAI/gobby/releases/download/"
            f"{spec.tag_prefix}{spec.floor_version}/{expected_asset}"
        )
        archive = _tar_with_binary(spec, payload=b"recovered")
        checksum = hashlib.sha256(archive).hexdigest().encode("ascii")
        page_two_url = "https://api.github.com/repos/GobbyAI/gobby/releases?per_page=100&page=2"
        calls: list[str] = []

        def fake_urlopen(req: Any, **_kwargs: Any) -> _JsonResponse | _BytesResponse:
            calls.append(req.full_url)
            if req.full_url.endswith("releases?per_page=100"):
                return _JsonResponse(
                    [{"tag_name": f"other-v{index}"} for index in range(100)],
                    link=f'<{page_two_url}>; rel="next"',
                )
            if req.full_url == page_two_url:
                return _JsonResponse(
                    [
                        {
                            "tag_name": f"{spec.tag_prefix}{spec.floor_version}",
                            "draft": False,
                            "prerelease": False,
                            "published_at": "2026-01-01T00:00:00Z",
                            "assets": [
                                {
                                    "name": expected_asset,
                                    "browser_download_url": asset_url,
                                }
                            ],
                        }
                    ]
                )
            if req.full_url == f"{asset_url}.sha256":
                return _BytesResponse(checksum)
            if req.full_url == asset_url:
                return _BytesResponse(archive)
            raise AssertionError(f"unexpected URL: {req.full_url}")

        monkeypatch.setattr("gobby.install.bin_freshness_github._urlopen_https", fake_urlopen)
        monkeypatch.setattr("gobby.install.bin_freshness_updater.platform_target", lambda: target)
        bin_dir = tmp_path / "bin"
        binary_path = _write_binary(bin_dir, spec)
        _write_stamp(bin_dir, spec, "0.4.0")

        record = update_managed_bin(
            postgres_db,
            spec,
            BinFreshnessConfig(),
            bin_dir=bin_dir,
            client=GithubReleaseClient(timeout_seconds=1),
        )

        assert record is not None
        assert record.last_status == "updated"
        assert binary_path.read_bytes() == b"recovered"
        assert calls == [
            "https://api.github.com/repos/GobbyAI/gobby/releases?per_page=100",
            page_two_url,
            f"{asset_url}.sha256",
            asset_url,
        ]

    def test_resolve_latest_asset_fails_closed_when_canonical_asset_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _spec()
        target = "aarch64-apple-darwin"
        expected_asset = f"{spec.name}-{target}.tar.gz"
        calls: list[str] = []

        def fake_urlopen(req: Any, **_kwargs: Any) -> _JsonResponse:
            calls.append(req.full_url)
            return _JsonResponse(
                [
                    {
                        "tag_name": "ghook-v0.4.3",
                        "draft": False,
                        "prerelease": False,
                        "published_at": "2026-07-01T00:00:00Z",
                        "assets": [],
                    }
                ]
            )

        monkeypatch.setattr("gobby.install.bin_freshness_github._urlopen_https", fake_urlopen)
        client = GithubReleaseClient(timeout_seconds=1)

        with pytest.raises(SourceUnavailableError, match=expected_asset):
            client.resolve_latest_asset(spec, target=target)

        assert calls == [
            "https://api.github.com/repos/GobbyAI/gobby/releases?per_page=100",
        ]


TEST_MACHINE_ID = "8fa1247f-e924-4bd7-a54e-b9dd5704304a"


@pytest.fixture(autouse=True)
def _isolate_machine_identity(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.install.bin_freshness_updater.get_machine_id",
        lambda: TEST_MACHINE_ID,
    )
    if "postgres_db" in request.fixturenames:
        database = request.getfixturevalue("postgres_db")
        LocalMachineManager(database).upsert_seen(TEST_MACHINE_ID, TEST_USER_ID)
