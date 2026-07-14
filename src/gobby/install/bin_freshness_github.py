"""GitHub Releases client for managed native binary updater."""

from __future__ import annotations

import hashlib
import hmac
import json
import platform
import sys
from http.client import HTTPException
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from gobby.install.bin_freshness_models import (
    ManagedBinSpec,
    ReleaseAsset,
    parse_version_tuple,
)
from gobby.install.checksums import parse_sha256_digest

_HELPER_RELEASE_REPOSITORY = "GobbyAI/gobby"
_MAX_RELEASE_PAGES = 10
_MAX_RELEASE_METADATA_BYTES = 8 * 1024 * 1024
_MAX_RELEASE_ASSET_BYTES = 128 * 1024 * 1024
_MAX_CHECKSUM_BYTES = 16 * 1024
_PLATFORM_TARGETS: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "aarch64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "amd64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("win32", "amd64"): "x86_64-pc-windows-msvc",
    ("win32", "x86_64"): "x86_64-pc-windows-msvc",
    ("win32", "arm64"): "aarch64-pc-windows-msvc",
}


class GithubAPIError(Exception):
    """GitHub metadata or artifact download failed unexpectedly."""


class SourceUnavailableError(Exception):
    """The expected release tag or platform asset is unavailable."""


def platform_target() -> str:
    """Return the release target triple for the current platform."""
    machine = platform.machine().lower()
    target = _PLATFORM_TARGETS.get((sys.platform, machine))
    if target is None:
        raise SourceUnavailableError(f"unsupported native binary target: {sys.platform}/{machine}")
    return target


def release_archive_extension(target: str) -> str:
    """Return release archive extension for a target triple."""
    return "zip" if "windows" in target else "tar.gz"


def release_asset_name(spec: ManagedBinSpec, target: str) -> str:
    """Return the expected release asset filename for a tool and target."""
    return f"{spec.artifact_name}-{target}.{release_archive_extension(target)}"


def _urlopen_https(req: Request, *, timeout: float) -> Any:
    parsed = urlparse(req.full_url)
    if parsed.scheme != "https":
        raise ValueError(f"Only HTTPS URLs are allowed, got: {parsed.scheme}://...")
    return urlopen(req, timeout=timeout)  # nosec B310 # scheme validated above


def _read_limited(response: Any, *, max_bytes: int, label: str) -> bytes:
    payload = response.read(max_bytes + 1)
    if not isinstance(payload, bytes):
        raise GithubAPIError(f"{label} returned an unexpected payload")
    if len(payload) > max_bytes:
        raise GithubAPIError(f"{label} exceeds the {max_bytes}-byte download limit")
    return payload


class GithubReleaseClient:
    """Minimal GitHub Releases API client."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_releases(self) -> list[dict[str, Any]]:
        """Fetch release metadata from GitHub."""
        return self._fetch_releases(_HELPER_RELEASE_REPOSITORY)

    def _fetch_releases(self, repository: str) -> list[dict[str, Any]]:
        """Fetch release metadata from a helper release repository."""
        releases: list[dict[str, Any]] = []
        url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
        expected_path = f"/repos/{repository}/releases"
        for _page in range(_MAX_RELEASE_PAGES):
            req = Request(
                url,
                headers={
                    "User-Agent": "gobby-daemon-bin-updater/1.0",
                    "Accept": "application/vnd.github+json",
                },
            )
            try:
                with _urlopen_https(req, timeout=self.timeout_seconds) as resp:
                    release_bytes = _read_limited(
                        resp,
                        max_bytes=_MAX_RELEASE_METADATA_BYTES,
                        label="GitHub Releases API response",
                    )
                    payload = json.loads(release_bytes.decode("utf-8"))
                    link_header = getattr(resp, "headers", {}).get("Link")
            except (URLError, HTTPException, OSError, ValueError) as exc:
                raise GithubAPIError(str(exc)) from exc
            if not isinstance(payload, list):
                raise GithubAPIError("GitHub Releases API returned an unexpected payload")
            releases.extend(release for release in payload if isinstance(release, dict))
            next_url = self._next_release_page_url(link_header, expected_path=expected_path)
            if next_url is None:
                return releases
            url = next_url
        raise GithubAPIError(f"GitHub Releases API exceeded {_MAX_RELEASE_PAGES} pages")

    @staticmethod
    def _next_release_page_url(link_header: object, *, expected_path: str) -> str | None:
        """Return a validated GitHub ``rel=next`` pagination URL."""
        if not isinstance(link_header, str):
            return None
        for link in link_header.split(","):
            parts = [part.strip() for part in link.split(";")]
            if 'rel="next"' not in parts[1:]:
                continue
            url_part = parts[0]
            if not (url_part.startswith("<") and url_part.endswith(">")):
                raise GithubAPIError("GitHub Releases API returned an invalid pagination link")
            url = url_part[1:-1]
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "api.github.com"
                or parsed.path != expected_path
            ):
                raise GithubAPIError("GitHub Releases API returned an invalid pagination URL")
            return url
        return None

    def resolve_latest_asset(self, spec: ManagedBinSpec, *, target: str) -> ReleaseAsset:
        """Resolve the newest stable release asset matching ``spec`` and ``target``."""
        try:
            release = self._resolve_latest_release_from(
                spec,
                self._fetch_releases(_HELPER_RELEASE_REPOSITORY),
            )
        except GithubAPIError as exc:
            raise SourceUnavailableError(
                f"no stable release asset found for tag prefix {spec.tag_prefix!r} "
                f"({_HELPER_RELEASE_REPOSITORY}: {exc})"
            ) from exc
        if release is None:
            raise SourceUnavailableError(
                f"no stable release asset found for tag prefix {spec.tag_prefix!r}"
            )
        return self._release_asset_from_release(spec, target=target, release=release)

    def _release_asset_from_release(
        self,
        spec: ManagedBinSpec,
        *,
        target: str,
        release: dict[str, Any],
    ) -> ReleaseAsset:
        """Extract the expected platform asset from one GitHub release."""
        tag_name = release.get("tag_name")
        if not isinstance(tag_name, str):
            raise SourceUnavailableError(f"{spec.name}: release is missing tag_name")
        expected_asset = release_asset_name(spec, target)
        assets = release.get("assets")
        if not isinstance(assets, list):
            raise SourceUnavailableError(f"{tag_name}: release assets are unavailable")
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("name") != expected_asset:
                continue
            url = asset.get("browser_download_url")
            if not isinstance(url, str) or not url:
                raise SourceUnavailableError(f"{tag_name}: asset {expected_asset} has no URL")
            return ReleaseAsset(
                tag_name=tag_name,
                version=tag_name[len(spec.tag_prefix) :],
                asset_name=expected_asset,
                asset_url=url,
                target=target,
            )
        raise SourceUnavailableError(f"{tag_name}: missing platform asset {expected_asset}")

    def download_asset(self, asset: ReleaseAsset) -> bytes:
        """Download and verify a resolved release asset."""
        headers = {"User-Agent": "gobby-daemon-bin-updater/1.0"}
        checksum_url = f"{asset.asset_url}.sha256"
        try:
            with _urlopen_https(
                Request(checksum_url, headers=headers), timeout=self.timeout_seconds
            ) as resp:
                checksum_bytes = _read_limited(
                    resp,
                    max_bytes=_MAX_CHECKSUM_BYTES,
                    label="GitHub checksum download",
                )
            expected = parse_sha256_digest(checksum_bytes.decode("utf-8"))
            if expected is None:
                raise GithubAPIError(f"invalid SHA-256 checksum at {checksum_url}")
            with _urlopen_https(
                Request(asset.asset_url, headers=headers), timeout=self.timeout_seconds
            ) as resp:
                payload = _read_limited(
                    resp,
                    max_bytes=_MAX_RELEASE_ASSET_BYTES,
                    label="GitHub asset download",
                )
        except (URLError, HTTPException, OSError, ValueError) as exc:
            raise GithubAPIError(str(exc)) from exc
        actual = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise GithubAPIError(
                f"checksum mismatch for {asset.asset_name}: expected {expected}, got {actual}"
            )
        return payload

    def _resolve_latest_release_from(
        self,
        spec: ManagedBinSpec,
        releases: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        stable_matches: list[tuple[tuple[int, ...] | None, str, dict[str, Any]]] = []
        for release in releases:
            if release.get("draft") or release.get("prerelease"):
                continue
            tag_name = release.get("tag_name")
            if not isinstance(tag_name, str) or not tag_name.startswith(spec.tag_prefix):
                continue
            published_at = release.get("published_at")
            published_sort = published_at if isinstance(published_at, str) else ""
            stable_matches.append(
                (parse_version_tuple(tag_name[len(spec.tag_prefix) :]), published_sort, release)
            )
        if not stable_matches:
            return None
        semver_matches = [match for match in stable_matches if match[0] is not None]
        if semver_matches:
            return max(semver_matches, key=lambda item: (item[0] or (), item[1]))[2]
        return max(stable_matches, key=lambda item: item[1])[2]


__all__ = [
    "GithubAPIError",
    "GithubReleaseClient",
    "SourceUnavailableError",
    "platform_target",
    "release_archive_extension",
    "release_asset_name",
]
