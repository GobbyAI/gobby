"""Deterministic source-content inventories used by hub backup verification."""

from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any

import click

from gobby.cli.hub_backup._integrity import open_regular_binary


@dataclass(frozen=True)
class ContentInventory:
    """Count and aggregate digest of canonical content records."""

    members: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {"members": self.members, "sha256": self.sha256}


def qdrant_collection_digest(client: Any, collection: str) -> str:
    """Hash every point id, payload, and vector in a collection."""
    point_digests: list[bytes] = []
    offset: object | None = None
    while True:
        batch, next_offset = client.scroll(
            collection,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        point_digests.extend(_point_digest(point) for point in batch)
        if next_offset is None:
            break
        if next_offset == offset:
            raise click.ClickException(
                f"Qdrant content inventory did not advance for collection {collection}"
            )
        offset = next_offset
    return _aggregate_digest(sorted(point_digests))


def qdrant_points_digest(points: list[object]) -> str:
    """Hash canonical JSON records independent of Qdrant scroll order."""
    return _aggregate_digest(sorted(_point_digest(point) for point in points))


def _point_digest(point: object) -> bytes:
    return hashlib.sha256(_canonical_point(point)).digest()


def archive_inventory(path: Path, *, label: str) -> ContentInventory:
    """Inventory regular-file bytes and directory names in a gzip tar archive."""
    try:
        with open_regular_binary(path, label=label) as source:
            with tarfile.open(fileobj=source, mode="r:gz") as archive:
                return _tar_inventory(archive, label=label)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise click.ClickException(f"{label} cannot be inventoried: {exc}") from exc


def tar_stream_inventory(stream: IO[bytes], *, label: str) -> ContentInventory:
    """Inventory a streaming uncompressed tar without buffering volume contents."""
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            return _tar_inventory(archive, label=label)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise click.ClickException(f"{label} cannot be inventoried: {exc}") from exc


def _canonical_point(point: object) -> bytes:
    raw: object
    if isinstance(point, dict):
        raw = point
    else:
        model_dump = getattr(point, "model_dump", None)
        if not callable(model_dump):
            raise click.ClickException(
                f"Qdrant returned an unsupported point representation: {type(point).__name__}"
            )
        raw = model_dump(mode="json", by_alias=True)
    if not isinstance(raw, dict) or "id" not in raw:
        raise click.ClickException("Qdrant point content inventory requires a point id")
    value = {key: raw.get(key) for key in ("id", "payload", "vector")}
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise click.ClickException(f"Qdrant point cannot be canonicalized: {exc}") from exc


def _tar_inventory(archive: tarfile.TarFile, *, label: str) -> ContentInventory:
    records: list[bytes] = []
    for member in archive:
        name = _safe_member_name(member, label=label)
        record: dict[str, object]
        if member.isdir():
            record = {"path": name, "type": "directory"}
        elif member.isfile():
            source = archive.extractfile(member)
            if source is None:
                raise click.ClickException(f"{label} could not read regular file member {name}")
            digest = hashlib.sha256()
            size = 0
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            if size != member.size:
                raise click.ClickException(
                    f"{label} member {name} is truncated: header {member.size}, read {size}"
                )
            record = {
                "path": name,
                "type": "file",
                "size": size,
                "sha256": digest.hexdigest(),
            }
        elif member.issym():
            raise click.ClickException(f"{label} refuses symlink member: {member.name}")
        elif member.islnk():
            raise click.ClickException(f"{label} refuses hard-link member: {member.name}")
        else:
            raise click.ClickException(f"{label} refuses special member: {member.name}")
        records.append(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
    if not records:
        raise click.ClickException(f"{label} contains 0 members; archive is empty or corrupt")
    return ContentInventory(members=len(records), sha256=_aggregate_digest(sorted(records)))


def _safe_member_name(member: tarfile.TarInfo, *, label: str) -> str:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise click.ClickException(f"{label} refuses unsafe member path: {member.name}")
    parts = [part for part in path.parts if part not in ("", ".")]
    return "/".join(parts) if parts else "."


def _aggregate_digest(records: list[bytes]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    return digest.hexdigest()
