"""GitHub Releases client for managed native binary updater."""

from __future__ import annotations

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

_RELEASES_URL = "https://api.github.com/repos/GobbyAI/gobby-cli/releases?per_page=100"
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


class GithubReleaseClient:
    """Minimal GitHub Releases API client."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_releases(self) -> list[dict[str, Any]]:
        """Fetch release metadata from GitHub."""
        req = Request(
            _RELEASES_URL,
            headers={
                "User-Agent": "gobby-daemon-bin-updater/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with _urlopen_https(req, timeout=self.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (URLError, HTTPException, OSError, ValueError) as exc:
            raise GithubAPIError(str(exc)) from exc
        if not isinstance(payload, list):
            raise GithubAPIError("GitHub Releases API returned an unexpected payload")
        return [release for release in payload if isinstance(release, dict)]

    def resolve_latest_asset(self, spec: ManagedBinSpec, *, target: str) -> ReleaseAsset:
        """Resolve the newest stable release asset matching ``spec`` and ``target``."""
        release = self._resolve_latest_release(spec)
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
        """Download a resolved release asset."""
        req = Request(asset.asset_url, headers={"User-Agent": "gobby-daemon-bin-updater/1.0"})
        try:
            with _urlopen_https(req, timeout=self.timeout_seconds) as resp:
                payload = resp.read()
        except (URLError, HTTPException, OSError, ValueError) as exc:
            raise GithubAPIError(str(exc)) from exc
        if not isinstance(payload, bytes):
            raise GithubAPIError("GitHub asset download returned an unexpected payload")
        return payload

    def _resolve_latest_release(self, spec: ManagedBinSpec) -> dict[str, Any]:
        stable_matches: list[tuple[tuple[int, ...] | None, str, dict[str, Any]]] = []
        for release in self.fetch_releases():
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
            raise SourceUnavailableError(
                f"no stable release found for tag prefix {spec.tag_prefix!r}"
            )
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
