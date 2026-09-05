"""Release discovery, verified downloads, and atomic native binary promotion."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from gobby.install.bin_freshness_github import SourceUnavailableError
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_freshness_promotion import stage_and_promote_release_binary
from gobby.install.checksums import parse_sha256_digest

logger = logging.getLogger(__name__)
_HELPER_RELEASE_REPOSITORY = "GobbyAI/gobby"


def _urlopen_https(req: Request, *, timeout: int) -> Any:
    """Open a URL after validating the scheme is HTTPS.

    Wraps :func:`urllib.request.urlopen` with a scheme check to prevent
    ``file://`` or other unexpected schemes (bandit B310).
    """
    url = req.full_url
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only HTTPS URLs are allowed, got: {parsed.scheme}://...")
    return urlopen(req, timeout=timeout)  # nosec B310 # scheme validated above


def _release_archive_extension(target: str) -> str:
    """Return the packaged release archive extension for a target triple."""
    return "zip" if "windows" in target else "tar.gz"


def _build_release_download_url(
    artifact_name: str,
    target: str,
    *,
    version: str | None,
    tag_prefix: str,
    resolved_tag: str | None = None,
) -> str:
    """Build the GitHub Releases download URL for a binary artifact."""
    archive_ext = _release_archive_extension(target)
    artifact_filename = f"{artifact_name}-{target}.{archive_ext}"
    tag_name = resolved_tag or (f"{tag_prefix}{version}" if version else None)
    if not tag_name:
        raise ValueError(f"No matching stable release found for tag prefix {tag_prefix!r}")
    return (
        f"https://github.com/{_HELPER_RELEASE_REPOSITORY}/releases/download/"
        f"{tag_name}/{artifact_filename}"
    )


def _parse_release_semver(version_text: str) -> tuple[int, ...] | None:
    """Parse a simple semver string into a sortable tuple when possible."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?", version_text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups(default="0"))


def _fetch_helper_releases(repository: str) -> list[dict[str, Any]]:
    """Fetch GitHub release metadata for a helper release repository."""
    req = Request(
        f"https://api.github.com/repos/{repository}/releases?per_page=100",
        headers={
            "User-Agent": "gobby-installer/1.0",
            "Accept": "application/vnd.github+json",
        },
    )
    with _urlopen_https(req, timeout=30) as resp:
        releases = json.loads(resp.read().decode("utf-8"))

    if not isinstance(releases, list):
        raise ValueError("GitHub Releases API returned an unexpected payload")
    return [release for release in releases if isinstance(release, dict)]


def _newest_stable_release_tag(
    releases: list[dict[str, Any]],
    *,
    tag_prefix: str,
) -> str | None:
    """Return the newest stable release tag in a release payload."""

    stable_matches: list[tuple[str, str]] = []
    semver_matches: list[tuple[tuple[int, ...], str, str]] = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag_name = release.get("tag_name")
        if not isinstance(tag_name, str) or not tag_name.startswith(tag_prefix):
            continue
        published_at = release.get("published_at")
        published_sort = published_at if isinstance(published_at, str) else ""
        stable_matches.append((tag_name, published_sort))
        semver = _parse_release_semver(tag_name[len(tag_prefix) :])
        if semver is not None:
            semver_matches.append((semver, published_sort, tag_name))

    if semver_matches:
        return max(semver_matches, key=lambda item: (item[0], item[1]))[2]
    if stable_matches:
        return max(stable_matches, key=lambda item: item[1])[0]
    return None


def _resolve_latest_release_tag(*, tag_prefix: str) -> str:
    """Resolve the newest stable GitHub release tag for a tag prefix."""
    try:
        tag = _newest_stable_release_tag(
            _fetch_helper_releases(_HELPER_RELEASE_REPOSITORY),
            tag_prefix=tag_prefix,
        )
    except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"No matching stable release found for tag prefix {tag_prefix!r} "
            f"({_HELPER_RELEASE_REPOSITORY}: {exc})"
        ) from exc
    if tag is None:
        raise ValueError(f"No matching stable release found for tag prefix {tag_prefix!r}")
    return tag


def _extract_binary_from_release_archive(
    archive_bytes: bytes,
    *,
    archive_ext: str,
    binary_name: str,
    bin_dir: Path,
    label: str,
) -> bool:
    """Extract one binary from a release archive into ``bin_dir``."""
    lock = try_acquire_native_bin_lock(label, bin_dir=bin_dir)
    if lock is None:
        logger.warning("%s: native binary update is already in progress", label)
        return False
    try:
        with lock:
            stage_and_promote_release_binary(
                archive_bytes,
                archive_ext=archive_ext,
                binary_name=binary_name,
                bin_dir=bin_dir,
                asset_name=label,
            )
        return True
    except (OSError, SourceUnavailableError) as e:
        logger.warning("%s: failed extracting release archive: %s", label, e)
        return False


def _fetch_release_checksum(checksum_url: str, *, label: str) -> str | None:
    """Fetch and parse the published SHA-256 digest for a release asset.

    Returns the lowercase hex digest, or ``None`` when the checksum file
    cannot be retrieved or parsed; the caller treats ``None`` as a
    verification failure (fail-closed).
    """
    try:
        req = Request(checksum_url, headers={"User-Agent": "gobby-installer/1.0"})
        with _urlopen_https(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
    except (URLError, OSError, ValueError) as e:
        logger.warning("%s: could not fetch checksum %s: %s", label, checksum_url, e)
        return None
    return parse_sha256_digest(text)


def _verify_release_artifact(
    archive_bytes: bytes,
    *,
    checksum_url: str,
    label: str,
) -> bool:
    """Verify downloaded artifact bytes against the published SHA-256.

    Fetches the per-asset ``.sha256`` file at ``checksum_url``, parses the
    expected digest, and compares it to the SHA-256 of ``archive_bytes``.
    Returns ``False`` — fail-closed — when the checksum cannot be fetched or
    parsed, or when the digests differ, so the caller falls through to the
    next install method instead of executing an unverified binary.
    """
    expected = _fetch_release_checksum(checksum_url, label=label)
    if expected is None:
        logger.warning(
            "%s: no published checksum at %s; refusing unverified download",
            label,
            checksum_url,
        )
        return False
    actual = hashlib.sha256(archive_bytes).hexdigest()
    if actual != expected:
        logger.warning(
            "%s: checksum mismatch for %s (expected %s, got %s)",
            label,
            checksum_url,
            expected,
            actual,
        )
        return False
    return True


def _download_release_binary(
    bin_dir: Path,
    *,
    binary_name: str,
    artifact_name: str,
    target: str,
    version: str | None,
    tag_prefix: str,
    label: str,
) -> bool:
    """Download and extract a native binary from GitHub Releases."""
    archive_ext = _release_archive_extension(target)
    try:
        resolved_tag = (
            _resolve_latest_release_tag(tag_prefix=tag_prefix) if version is None else None
        )
        url = _build_release_download_url(
            artifact_name,
            target,
            version=version,
            tag_prefix=tag_prefix,
            resolved_tag=resolved_tag,
        )
        logger.info("Downloading %s from %s", label, url)
        req = Request(url, headers={"User-Agent": "gobby-installer/1.0"})
        with _urlopen_https(req, timeout=30) as resp:
            archive_bytes = resp.read()
        if not _verify_release_artifact(
            archive_bytes,
            checksum_url=f"{url}.sha256",
            label=label,
        ):
            return False
        return _extract_binary_from_release_archive(
            archive_bytes,
            archive_ext=archive_ext,
            binary_name=binary_name,
            bin_dir=bin_dir,
            label=label,
        )
    except (URLError, OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning("%s: GitHub download failed: %s", label, e)
        return False
