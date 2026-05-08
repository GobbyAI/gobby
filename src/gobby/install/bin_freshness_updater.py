"""GitHub-backed updater for Gobby-managed native binaries."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.install.bin_freshness_github import (
    GithubAPIError,
    GithubReleaseClient,
    SourceUnavailableError,
    platform_target,
    release_archive_extension,
)
from gobby.install.bin_freshness_inspector import inspect_managed_bin
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_freshness_models import (
    BinInspection,
    ManagedBinSpec,
    ReleaseAsset,
    compare_versions,
    managed_bin_specs,
)
from gobby.storage.bin_update_state import BinUpdateRecord, BinUpdateStateStore
from gobby.storage.database import DatabaseProtocol
from gobby.utils.native_bin import native_bin_dir

logger = logging.getLogger(__name__)


def update_all_managed_bins(
    db: DatabaseProtocol,
    config: BinFreshnessConfig,
    *,
    bin_dir: Path | None = None,
    client: GithubReleaseClient | None = None,
) -> list[BinUpdateRecord]:
    """Run one freshness cycle for all managed native binaries."""
    if not config.enabled:
        return []
    root = bin_dir or native_bin_dir()
    store = BinUpdateStateStore(db)
    records: list[BinUpdateRecord] = []
    release_client = client or GithubReleaseClient(timeout_seconds=config.github_timeout_seconds)
    for spec in managed_bin_specs():
        try:
            record = update_managed_bin(
                db,
                spec,
                config,
                bin_dir=root,
                client=release_client,
            )
        except Exception as exc:
            logger.exception("%s: managed binary update failed", spec.name)
            inspection = inspect_managed_bin(spec, bin_dir=root)
            record = _record_state(
                store,
                inspection=inspection,
                latest_version=None,
                target=None,
                status="failed",
                error=str(exc),
                source_url=None,
            )
        if record is not None:
            records.append(record)
    return records


def update_managed_bin(
    db: DatabaseProtocol,
    spec: ManagedBinSpec,
    config: BinFreshnessConfig,
    *,
    bin_dir: Path | None = None,
    client: GithubReleaseClient | None = None,
) -> BinUpdateRecord | None:
    """Run one freshness check/update for a managed native binary.

    Returns ``None`` only when the per-tool lock is already held; lock-held
    cycles intentionally leave the previous DB state untouched.
    """
    root = bin_dir or native_bin_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock = try_acquire_native_bin_lock(spec.name, bin_dir=root)
    if lock is None:
        logger.debug("%s: update skipped because another updater holds the lock", spec.name)
        return None

    with lock:
        store = BinUpdateStateStore(db)
        inspection = inspect_managed_bin(spec, bin_dir=root)
        if inspection.is_dev:
            return _record_state(
                store,
                inspection=inspection,
                latest_version=None,
                target=None,
                status="dev",
                error=None,
                source_url=None,
            )

        target: str | None = None
        release_client = client or GithubReleaseClient(
            timeout_seconds=config.github_timeout_seconds
        )
        try:
            target = platform_target()
            asset = release_client.resolve_latest_asset(spec, target=target)
        except SourceUnavailableError as exc:
            status = "floor_violated" if inspection.floor_drift else "source_unavailable"
            return _record_state(
                store,
                inspection=inspection,
                latest_version=None,
                target=target,
                status=status,
                error=str(exc),
                source_url=None,
            )
        except GithubAPIError as exc:
            return _record_state(
                store,
                inspection=inspection,
                latest_version=None,
                target=target,
                status="failed",
                error=str(exc),
                source_url=None,
            )

        if _is_up_to_date(inspection, asset):
            return _record_state(
                store,
                inspection=inspection,
                latest_version=asset.version,
                target=target,
                status="up_to_date",
                error=inspection.sidecar_error,
                source_url=asset.asset_url,
            )

        try:
            _stage_and_promote(release_client, spec, asset, root)
        except SourceUnavailableError as exc:
            status = "floor_violated" if inspection.floor_drift else "source_unavailable"
            return _record_state(
                store,
                inspection=inspection,
                latest_version=asset.version,
                target=target,
                status=status,
                error=str(exc),
                source_url=asset.asset_url,
            )
        except GithubAPIError as exc:
            return _record_state(
                store,
                inspection=inspection,
                latest_version=asset.version,
                target=target,
                status="failed",
                error=str(exc),
                source_url=asset.asset_url,
            )
        except OSError as exc:
            return _record_state(
                store,
                inspection=inspection,
                latest_version=asset.version,
                target=target,
                status="failed",
                error=str(exc),
                source_url=asset.asset_url,
            )

        updated = inspect_managed_bin(spec, bin_dir=root)
        return _record_state(
            store,
            inspection=updated,
            latest_version=asset.version,
            target=target,
            status="updated",
            error=None,
            source_url=asset.asset_url,
        )


def _is_up_to_date(inspection: BinInspection, asset: ReleaseAsset) -> bool:
    if inspection.floor_drift:
        return False
    comparison = compare_versions(inspection.installed_version, asset.version)
    return comparison is not None and comparison >= 0


def _stage_and_promote(
    client: GithubReleaseClient,
    spec: ManagedBinSpec,
    asset: ReleaseAsset,
    bin_dir: Path,
) -> None:
    archive_bytes = client.download_asset(asset)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{spec.name}-staging-", dir=str(bin_dir)))
    try:
        staged_binary = _extract_binary_to_staging(
            archive_bytes,
            spec=spec,
            asset=asset,
            staging_dir=staging_dir,
        )
        os.replace(staged_binary, bin_dir / spec.binary_name)
        _write_atomic_text(bin_dir / spec.stamp_name, f"{asset.version}\n", mode=0o644)
        _write_install_sidecar(spec, asset, bin_dir)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _extract_binary_to_staging(
    archive_bytes: bytes,
    *,
    spec: ManagedBinSpec,
    asset: ReleaseAsset,
    staging_dir: Path,
) -> Path:
    archive_ext = release_archive_extension(asset.target)
    dest = staging_dir / spec.binary_name
    try:
        if archive_ext == "zip":
            with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
                for member_name in archive.namelist():
                    if (
                        member_name.endswith(f"/{spec.binary_name}")
                        or member_name == spec.binary_name
                    ):
                        with archive.open(member_name) as fileobj:
                            dest.write_bytes(fileobj.read())
                        dest.chmod(0o755)
                        return dest
        else:
            with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if (
                        member.name.endswith(f"/{spec.binary_name}")
                        or member.name == spec.binary_name
                    ):
                        extracted_file = archive.extractfile(member)
                        if extracted_file is None:
                            continue
                        dest.write_bytes(extracted_file.read())
                        dest.chmod(0o755)
                        return dest
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise SourceUnavailableError(f"{asset.asset_name}: extraction failed: {exc}") from exc
    raise SourceUnavailableError(f"{asset.asset_name}: binary {spec.binary_name} not found")


def _write_install_sidecar(spec: ManagedBinSpec, asset: ReleaseAsset, bin_dir: Path) -> None:
    installed_at = datetime.now(UTC).isoformat()
    payload = {
        "install_method": "github-release",
        "install_source_url": asset.asset_url,
        "installed_version": asset.version,
        "installed_at": installed_at,
        "tag_name": asset.tag_name,
        "target": asset.target,
    }
    _write_atomic_text(
        bin_dir / spec.sidecar_name,
        json.dumps(payload, sort_keys=True) + "\n",
        mode=0o644,
    )


def _write_atomic_text(path: Path, value: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fileobj:
            fileobj.write(value)
            fileobj.flush()
            os.fsync(fileobj.fileno())
            os.fchmod(fileobj.fileno(), mode)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _record_state(
    store: BinUpdateStateStore,
    *,
    inspection: BinInspection,
    latest_version: str | None,
    target: str | None,
    status: str,
    error: str | None,
    source_url: str | None,
) -> BinUpdateRecord:
    return store.upsert(
        tool_name=inspection.spec.name,
        installed_version=inspection.installed_version,
        floor_version=inspection.spec.floor_version,
        latest_version=latest_version,
        binary_path=inspection.binary_path
        if inspection.binary_exists or inspection.is_dev
        else None,
        target=target,
        last_status=status,  # type: ignore[arg-type]
        last_error=error,
        installed_at=inspection.installed_at,
        source_url=source_url,
        is_dev=inspection.is_dev,
        floor_drift=inspection.floor_drift,
    )


__all__ = ["update_all_managed_bins", "update_managed_bin"]
