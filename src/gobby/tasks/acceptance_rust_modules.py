"""Committed Rust module identities for acceptance-test transcript matching."""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath

from gobby.utils.daemon_git import GitFailed, GitOk, daemon_git

# Consume comments and literals before looking for real module declarations.
_MODULE_TOKEN_RE = re.compile(
    r'//[^\n]*|/\*.*?\*/|r(?P<hashes>\#*)".*?"(?P=hashes)|"(?:\\.|[^"\\])*"'
    r"|'(?:\\(?:u\{[0-9A-Fa-f]+\}|.)|[^'\\])'"
    r'|(?:#\s*\[\s*path\s*=\s*"(?P<path>[^"\n]+)"\s*\]'
    r"\s*(?:#\s*\[[^\]]*\]\s*)*)?(?:pub(?:\([^)]*\))?\s+)?"
    r"\bmod\s+(?P<module>[A-Za-z_]\w*)\s*(?P<delimiter>[{;])"
    r"|\bfn\s+(?P<function>[A-Za-z_]\w*)|(?P<brace>[{}])",
    re.DOTALL,
)


def rust_inline_test_module(source: str, symbol: str) -> tuple[str, ...]:
    """Find the enclosing inline modules of the uniquely resolved test function."""
    name = symbol.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    stack: list[str | None] = []
    for match in _MODULE_TOKEN_RE.finditer(source):
        if match.group("function") == name:
            return tuple(part for part in stack if part is not None)
        if match.group("module") is not None and match.group("delimiter") == "{":
            stack.append(match.group("module"))
        elif match.group("brace") == "{":
            stack.append(None)
        elif match.group("brace") == "}" and stack:
            stack.pop()
    return ()


type RustModuleDeclarations = dict[str, set[tuple[str, tuple[str, ...]]]]
type RustModuleCache = dict[tuple[str, str], RustModuleDeclarations]


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


async def _read_declarations(commit_sha: str, root: str, repo_path: str) -> RustModuleDeclarations:
    """Read only files containing path attributes from the close candidate."""
    result = await daemon_git.run(
        (
            "grep",
            "-l",
            "-E",
            r"#[[:space:]]*\[[[:space:]]*path([[:space:]]|=|$)",
            commit_sha,
            "--",
            f"{root}/*.rs",
        ),
        cwd=repo_path,
        timeout=30,
    )
    if isinstance(result, GitFailed) and result.returncode == 1:
        return {}
    if not isinstance(result, GitOk):
        raise RuntimeError("could not read committed Rust module declarations")
    declarations: RustModuleDeclarations = {}
    for line in result.stdout.splitlines():
        declaring_path = line.removeprefix(f"{commit_sha}:")
        source = await daemon_git.run(
            ("show", f"{commit_sha}:{declaring_path}"), cwd=repo_path, timeout=30
        )
        if not isinstance(source, GitOk):
            raise RuntimeError(f"could not read committed Rust module {declaring_path}")
        _collect_declarations(declarations, declaring_path, source.stdout)
    return declarations


def _collect_declarations(
    declarations: RustModuleDeclarations, declaring_path: str, source: str
) -> None:
    """Track inline names and the Rust path-attribute directory rules."""
    owner = PurePosixPath(declaring_path)
    inline_root = owner.parent if owner.stem in {"lib", "main", "mod"} else owner.with_suffix("")
    stack: list[tuple[str | None, PurePosixPath]] = []
    for match in _MODULE_TOKEN_RE.finditer(source):
        name = match.group("module")
        path = match.group("path")
        directory = stack[-1][1] if stack else owner.parent
        if name is not None:
            if match.group("delimiter") == "{":
                if path is None:
                    has_inline_module = any(part is not None for part, _ in stack)
                    directory = (directory if has_inline_module else inline_root) / name
                else:
                    directory = directory / path
                stack.append((name, directory))
            elif path is not None:
                target = posixpath.normpath(str(directory / path))
                inline_names = tuple(part for part, _ in stack if part is not None)
                declarations.setdefault(target, set()).add((declaring_path, (*inline_names, name)))
        elif match.group("brace") == "{":
            stack.append((None, directory))
        elif match.group("brace") == "}" and stack:
            stack.pop()


async def resolve_rust_module(
    path: str, commit_sha: str, repo_path: str, *, cache: RustModuleCache | None = None
) -> tuple[str, ...]:
    """Share committed declarations per (candidate, crate) within one resolution."""
    parts = PurePosixPath(path).parts
    root = str(PurePosixPath(*parts[:2])) if parts[:1] == ("crates",) else "."
    key = (commit_sha, root)
    if cache is None:
        cache = {}
    if key not in cache:
        cache[key] = await _read_declarations(commit_sha, root, repo_path)
    declarations = cache[key]

    def module_for(source_path: str, seen: frozenset[str]) -> tuple[str, ...]:
        if source_path in seen:
            raise ValueError(f"cyclic Rust path module: {source_path}")
        if source_path not in declarations:
            return rust_source_module(source_path)
        if len(declarations[source_path]) != 1:
            raise ValueError(f"ambiguous Rust path module: {source_path}")
        owner, suffix = next(iter(declarations[source_path]))
        owner_module = (
            ()
            if PurePosixPath(owner).parent == PurePosixPath(root) / "tests"
            else module_for(owner, seen | {source_path})
        )
        return (*owner_module, *suffix)

    return module_for(path, frozenset())


def rust_evidence_names_test(
    evidence: str, path: str, symbol: str, rust_module: tuple[str, ...] | None
) -> bool:
    """Return whether one evidence line names a Rust test under its source module."""
    if PurePosixPath(path).suffix != ".rs":
        return False

    path_parts = rust_module if rust_module is not None else rust_source_module(path)
    source_parts = PurePosixPath(path).parts
    source_parts = source_parts[2:] if source_parts[:1] == ("crates",) else source_parts
    is_integration_test = bool(source_parts and source_parts[0] == "tests")
    if not path_parts:
        return False

    module_prefix = "::".join(re.escape(part) for part in path_parts)
    symbol = re.escape(symbol)
    module_test = re.compile(
        rf"(?<![\w:]){module_prefix}::{symbol}\b"
        if is_integration_test and rust_module is not None
        else rf"(?<![\w:]){module_prefix}(?:::[A-Za-z0-9_]+)*::{symbol}\b"
    )
    integration_stem = re.compile(rf"\b{re.escape(PurePosixPath(path).stem)}\b")
    symbol_word = re.compile(rf"\b{symbol}\b")

    return any(
        module_test.search(line)
        or (
            is_integration_test
            and len(source_parts) == 2
            and integration_stem.search(line)
            and symbol_word.search(line)
        )
        for line in evidence.splitlines()
    )
