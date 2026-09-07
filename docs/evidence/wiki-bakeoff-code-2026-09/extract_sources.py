"""Extract frozen third-party release archives without creating Git workspaces."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path, PurePosixPath

from provision_environment import DEFAULT_RUNTIME_ROOT, _sha256_file, _write_json, build_manifest
from validate_environment import PINS

ORIGINS = {
    "graphify": ("Graphify-Labs/graphify", "graphify-c9f9901.tar.gz"),
    "understand-anything": ("Egonex-AI/Understand-Anything", "understand-anything-07edf82.tar.gz"),
    "archify": ("tt-a1i/archify", "archify-c651940.tar.gz"),
    "codewiki": ("FSoft-AI4Code/CodeWiki", "codewiki-2584854.tar.gz"),
    "opendeepwiki": ("AIDotNet/OpenDeepWiki", "opendeepwiki-75840e5.tar.gz"),
}


def extract_source(root: Path, name: str) -> None:
    origin, filename = ORIGINS[name]
    pin = PINS[name]
    archive = root / "sources" / filename
    destination = root / "sources" / name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to overwrite {destination}")
    expected_prefix = f"{origin.split('/')[-1]}-{pin}"
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        total = 0
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != expected_prefix
                or not (member.isfile() or member.isdir())
                or ".git" in path.parts
            ):
                raise ValueError(f"unsafe or unexpected archive member: {member.name}")
            total += member.size
        if total > 1_000_000_000 or len(members) > 100_000:
            raise ValueError("archive exceeds extraction limits")
        destination.mkdir()
        for member in members:
            parts = PurePosixPath(member.name).parts[1:]
            if not parts:
                continue
            member.name = PurePosixPath(*parts).as_posix()
            stream.extract(member, destination, filter="data")
    manifest = build_manifest(destination, pin, [])
    _write_json(root / "manifests" / f"{name}-source.json", manifest)
    _write_json(
        root / "receipts" / f"{name}-source.json",
        {
            "required_pin": pin,
            "source_url": f"https://codeload.github.com/{origin}/tar.gz/{pin}",
            "archive": str(archive),
            "archive_sha256": _sha256_file(archive),
            "source_root": str(destination),
            "input_tree_sha256": manifest["input_tree_sha256"],
            "file_count": manifest["file_count"],
            "acquisition": "official commit archive; not a Git development workspace",
        },
    )
    print(f"{name}: {manifest['file_count']} files, archive SHA-256 {_sha256_file(archive)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", choices=sorted(ORIGINS))
    args = parser.parse_args()
    extract_source(DEFAULT_RUNTIME_ROOT, args.name)


if __name__ == "__main__":
    main()
