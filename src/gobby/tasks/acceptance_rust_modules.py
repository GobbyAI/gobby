"""Committed Rust module identities for acceptance-test transcript matching."""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath

from gobby.utils.daemon_git import GitFailed, GitOk, daemon_git

# Consume comments and literals before looking for real module declarations.
_PATH_MODULE_RE = re.compile(
    r'//[^\n]*|/\*.*?\*/|r(?P<hashes>\#*)".*?"(?P=hashes)|"(?:\\.|[^"\\])*"'
    r'|#\s*\[\s*path\s*=\s*"(?P<path>[^"\n]+)"\s*\]'
    r"\s*(?:#\s*\[[^\]]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?"
    r"mod\s+(?P<module>[A-Za-z_]\w*)\s*;",
    re.DOTALL,
)


def rust_source_module(path: str) -> tuple[str, ...]:
    """Derive the ordinary Rust module path, excluding crate source roots."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if len(parts) >= 3 and parts[0] == "crates":
        parts = parts[2:]
    if parts and parts[0] in {"src", "tests"}:
        parts = parts[1:]
    if parts and parts[-1] in {"lib", "main", "mod"}:
        parts.pop()
    return tuple(parts)


async def resolve_rust_module(path: str, commit_sha: str, repo_path: str) -> tuple[str, ...]:
    """Read path overrides from the close candidate, never the working tree."""
    parts = PurePosixPath(path).parts
    root = str(PurePosixPath(*parts[:2])) if parts[:1] == ("crates",) else "."
    result = await daemon_git.run(
        ("grep", "-l", "-E", r"path[[:space:]]*=", commit_sha, "--", f"{root}/*.rs"),
        cwd=repo_path,
        timeout=30,
    )
    if isinstance(result, GitFailed) and result.returncode == 1:
        return rust_source_module(path)
    if not isinstance(result, GitOk):
        raise RuntimeError("could not read committed Rust module declarations")
    declarations: dict[str, set[tuple[str, str]]] = {}
    for line in result.stdout.splitlines():
        declaring_path = line.removeprefix(f"{commit_sha}:")
        source = await daemon_git.run(
            ("show", f"{commit_sha}:{declaring_path}"), cwd=repo_path, timeout=30
        )
        if not isinstance(source, GitOk):
            raise RuntimeError(f"could not read committed Rust module {declaring_path}")
        for match in _PATH_MODULE_RE.finditer(source.stdout):
            if match.group("path") is None:
                continue
            target = posixpath.normpath(
                str(PurePosixPath(declaring_path).parent / match.group("path"))
            )
            declarations.setdefault(target, set()).add((declaring_path, match.group("module")))

    def module_for(source_path: str, seen: frozenset[str]) -> tuple[str, ...]:
        if source_path in seen:
            raise ValueError(f"cyclic Rust path module: {source_path}")
        if source_path not in declarations:
            return rust_source_module(source_path)
        if len(declarations[source_path]) != 1:
            raise ValueError(f"ambiguous Rust path module: {source_path}")
        owner, name = next(iter(declarations[source_path]))
        return (*module_for(owner, seen | {source_path}), name)

    return module_for(path, frozenset())
