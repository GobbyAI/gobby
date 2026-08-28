#!/usr/bin/env python3
"""Fail closed when gobby-client's pinned gobby-terminal version is unpublished.

Used by ``release-gclient.yml`` before ``cargo package`` / ``cargo publish``.
A successful ``gobby-terminal`` publication of version *V* must precede the
``gclient-v*`` tag that depends on *V*. Stdlib only so the Rust release job
can run this without installing the Gobby Python package.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CRATES_IO_VERSION_URL = "https://crates.io/api/v1/crates/{crate}/{version}"
USER_AGENT = "gobby-release-preflight/1.0"


class DependencyUnpublishedError(RuntimeError):
    """The required crates.io crate version is missing or yanked."""


def gobby_terminal_dependency_version(manifest_text: str) -> str:
    """Return the exact ``gobby-terminal`` version declared in a Cargo.toml."""
    manifest = tomllib.loads(manifest_text)
    dep = manifest.get("dependencies", {}).get("gobby-terminal")
    if not isinstance(dep, dict):
        raise ValueError("crates/gclient/Cargo.toml has no gobby-terminal table dependency")
    version = dep.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(
            "gobby-terminal dependency is missing an explicit version "
            "(required so cargo publish can rewrite the workspace path)"
        )
    return version.strip()


def crates_io_version_status(
    crate: str,
    version: str,
    *,
    opener: Any | None = None,
) -> str:
    """Return ``published``, ``unpublished``, or ``yanked`` for a crate version."""
    fetch = urlopen if opener is None else opener
    url = CRATES_IO_VERSION_URL.format(crate=crate, version=version)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with fetch(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return "unpublished"
        raise
    except URLError as exc:
        raise DependencyUnpublishedError(
            f"could not query crates.io for {crate} {version}: {exc}"
        ) from exc

    version_info = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version_info, dict):
        return "unpublished"
    if version_info.get("yanked") is True:
        return "yanked"
    return "published"


def require_gobby_terminal_on_crates_io(
    manifest_path: Path,
    *,
    opener: Any | None = None,
) -> str:
    """Return the published version, or raise if unpublished/yanked."""
    version = gobby_terminal_dependency_version(manifest_path.read_text(encoding="utf-8"))
    status = crates_io_version_status("gobby-terminal", version, opener=opener)
    if status == "published":
        return version
    raise DependencyUnpublishedError(
        f"gobby-terminal {version} is {status} on crates.io; "
        "publish gobby-terminal version V before tagging gclient-vV "
        f"(required by {manifest_path})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("crates/gclient/Cargo.toml"),
        help="Path to crates/gclient/Cargo.toml",
    )
    args = parser.parse_args(argv)
    try:
        version = require_gobby_terminal_on_crates_io(args.manifest)
    except (OSError, ValueError, DependencyUnpublishedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"gobby-terminal {version} is published on crates.io.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
