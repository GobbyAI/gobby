"""Verify exported Ask publication bytes and frozen-cohort provenance."""

from __future__ import annotations

import json
import tarfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from ask_cohort_records import (
    EXPECTED_TOOL_IDENTITIES,
    AttemptError,
    _mapping,
    _sha256_bytes,
    _sha256_file,
    _string,
)


def _safe_tar_members(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(path, "r:") as archive:
        for member in archive.getmembers():
            pure = PurePosixPath(member.name)
            if (
                member.name in files
                or pure.is_absolute()
                or ".." in pure.parts
                or not member.isfile()
            ):
                raise AttemptError(f"unsafe Ask export member: {member.name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AttemptError(f"unreadable Ask export member: {member.name}")
            files[member.name] = extracted.read()
    for required in ("manifest.json", "answer.json", "evidence-manifest.json"):
        if required not in files:
            raise AttemptError(f"Ask export is missing {required}")
    return files


def read_verified_publication(
    path: Path,
    *,
    expected_tar_sha256: object = None,
    expected_files: object = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, bytes]]:
    """Read a publication only after verifying its recorded and internal hashes."""
    if expected_tar_sha256 is not None and _sha256_file(path) != expected_tar_sha256:
        raise AttemptError("publication tar hash changed after execution")
    files = _safe_tar_members(path)
    publication = _mapping(json.loads(files["manifest.json"]), name="publication manifest")
    manifests = [publication.get("files")]
    if expected_files is not None:
        manifests.append(expected_files)
    for file_manifest in manifests:
        if not isinstance(file_manifest, list):
            raise AttemptError("publication file manifest is malformed")
        for raw in file_manifest:
            item = _mapping(raw, name="publication file entry")
            relative = _string(item.get("path"), name="publication file path")
            payload = files.get(relative)
            if payload is None or len(payload) != item.get("size_bytes"):
                raise AttemptError(f"publication file size mismatch: {relative}")
            if _sha256_bytes(payload) != item.get("sha256"):
                raise AttemptError(f"publication file hash mismatch: {relative}")
    answer = _mapping(json.loads(files["answer.json"]), name="published answer")
    evidence = _mapping(json.loads(files["evidence-manifest.json"]), name="evidence manifest")
    return publication, answer, evidence, files


def verify_export(
    path: Path,
    *,
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    question: Mapping[str, str],
) -> dict[str, Any]:
    publication, answer, evidence, files = read_verified_publication(path)
    run_id = _string(result.get("run_id"), name="Ask run_id")
    if publication.get("run_id") != run_id:
        raise AttemptError("publication run_id differs from the Ask result")
    provenance = _mapping(publication.get("provenance"), name="publication provenance")
    request = _mapping(provenance.get("request"), name="publication request")
    binding = _mapping(provenance.get("binding"), name="publication binding")
    if request.get("question") != question["question"] or "commit_ref" in request:
        raise AttemptError("publication request differs from the frozen question")
    if binding.get("commit_oid") != question["source_commit"]:
        raise AttemptError("publication source commit differs from the frozen question")
    if provenance.get("profiles") != manifest["runtime_identity"]["profiles"]:
        raise AttemptError("publication profile snapshots differ from pre-execution identities")
    if provenance.get("tool_identities") != list(EXPECTED_TOOL_IDENTITIES):
        raise AttemptError("publication tool identities differ from pre-execution identities")
    if answer.get("run_id") != run_id or answer.get("question") != question["question"]:
        raise AttemptError("published answer differs from the frozen run")
    snapshot = _mapping(evidence.get("repository_binding"), name="evidence repository binding")
    if evidence.get("project_id") != binding.get("project_id"):
        raise AttemptError("evidence authorization project mismatch")
    for key in ("commit_oid", "tree_oid"):
        if snapshot.get(key) != binding.get(key):
            raise AttemptError(f"evidence snapshot binding mismatch for {key}")
    manifest_hash = _sha256_bytes(files["manifest.json"])
    pointer = result.get("artifact_manifest")
    if isinstance(pointer, dict) and pointer.get("sha256") != manifest_hash:
        raise AttemptError("result publication manifest hash differs from exported bytes")
    return {
        "tar_path": str(path),
        "tar_sha256": _sha256_file(path),
        "file_count": len(files),
        "bytes": sum(len(payload) for payload in files.values()),
        "manifest_sha256": manifest_hash,
        "files": [
            {"path": name, "sha256": _sha256_bytes(payload), "size_bytes": len(payload)}
            for name, payload in sorted(files.items())
        ],
    }
