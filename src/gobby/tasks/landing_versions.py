"""Generate and verify landing-owned binary version changes."""

import ast
import os
import re
import subprocess
import tempfile
import tomllib
from collections.abc import Collection, Mapping
from pathlib import Path

from gobby.install import bin_release_identity as release
from gobby.utils import spawn
from gobby.utils.git import git_subprocess_env

_PINS = "src/gobby/install/version_pins.py"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _git(
    repo: Path, *args: str, content: str | None = None, env: Mapping[str, str] | None = None
) -> str:
    return spawn.run(
        ["git", "-C", str(repo), *args],
        input=content,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
        env=dict(env) if env is not None else git_subprocess_env(),
    ).stdout.rstrip("\n")


def _read(repo: Path, revision: str, path: str) -> str:
    entry = _git(repo, "ls-tree", revision, "--", path)
    if not entry.startswith("100644 blob "):
        raise release.BinaryReleaseVersionError(f"version input is not a regular file: {path}")
    # cat-file preserves the final newline, unlike the command-output helper.
    return spawn.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"],
        capture_output=True,
        check=True,
        timeout=30,
        env=git_subprocess_env(),
    ).stdout.decode("utf-8")


def _replace_once(pattern: str, replacement: str, text: str) -> str:
    result, count = re.subn(pattern, lambda _match: replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise release.BinaryReleaseVersionError(
            "version input must have exactly one matching field"
        )
    return result


def _package_version(text: str, old: str, new: str) -> str:
    return _replace_once(
        rf'^version[ \t]*=[ \t]*"{re.escape(old)}"[ \t]*$', f'version = "{new}"', text
    )


def _pin_version(text: str, name: str, old: str, new: str) -> str:
    tables: list[ast.Dict] = []
    value: ast.expr | None
    for statement in ast.parse(text).body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "MANAGED_BIN_VERSION_PINS"
            for target in targets
        ):
            if isinstance(value, ast.Dict):
                tables.append(value)
    if len(tables) != 1:
        raise release.BinaryReleaseVersionError("managed pins must have one literal dictionary")
    matches = [
        value
        for key, value in zip(tables[0].keys, tables[0].values, strict=True)
        if isinstance(key, ast.Constant) and key.value == name
    ]
    if len(matches) != 1 or not isinstance(matches[0], ast.Constant) or matches[0].value != old:
        raise release.BinaryReleaseVersionError(
            "managed binary pin must match the integration base"
        )
    value = matches[0]
    literal = ast.get_source_segment(text, value)
    if (
        literal not in (f'"{old}"', f"'{old}'")
        or value.end_lineno is None
        or value.end_col_offset is None
    ):
        raise release.BinaryReleaseVersionError("managed binary pin must be a plain version string")
    lines = text.encode("utf-8").splitlines(keepends=True)
    start = sum(map(len, lines[: value.lineno - 1])) + value.col_offset
    end = sum(map(len, lines[: value.end_lineno - 1])) + value.end_col_offset
    source = text.encode("utf-8")
    return (source[:start] + literal.replace(old, new).encode("utf-8") + source[end:]).decode(
        "utf-8"
    )


def _updates(repo: Path, base: str, candidate: str, paths: Collection[str]) -> dict[str, str]:
    binaries = release.affected_binaries(repo, base, candidate, paths)
    if not binaries:
        return {}
    lock = _read(repo, candidate, "Cargo.lock")
    pins = _read(repo, candidate, _PINS)
    updates: dict[str, str] = {}
    for name in binaries:
        manifest_path = f"crates/{release.RELEASE_CRATES[name]}/Cargo.toml"
        before = tomllib.loads(_read(repo, base, manifest_path))["package"]
        manifest = _read(repo, candidate, manifest_path)
        package = tomllib.loads(manifest)["package"]
        old = before["version"]
        if not isinstance(old, str):
            raise release.BinaryReleaseVersionError("binary version must be a literal string")
        if package["version"] != old:
            raise release.BinaryReleaseVersionError("binary versions are owned by landing")
        if re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", old) is None:
            raise release.BinaryReleaseVersionError(f"invalid binary version: {old}")
        major, minor, patch = old.split(".")
        new = f"{major}.{minor}.{int(patch) + 1}"
        start = manifest.index("[package]") + len("[package]")
        end_match = re.search(r"^\[", manifest[start:], re.MULTILINE)
        end = start + end_match.start() if end_match else len(manifest)
        updates[manifest_path] = (
            manifest[:start] + _package_version(manifest[start:end], old, new) + manifest[end:]
        )
        blocks = re.split(r"(?=^\[\[package\]\])", lock, flags=re.MULTILINE)
        matches = 0
        for index, block in enumerate(blocks):
            if not block.startswith("[[package]]"):
                continue
            locked = tomllib.loads(block)["package"][0]
            if locked["name"] == package["name"] and "source" not in locked:
                blocks[index] = _package_version(block, old, new)
                matches += 1
        if matches != 1:
            raise release.BinaryReleaseVersionError(
                "workspace binary missing or duplicated in lock"
            )
        lock = "".join(blocks)
        # Qualified dependency entries in Cargo.lock include the package version.
        lock = lock.replace(f'"{package["name"]} {old}"', f'"{package["name"]} {new}"')
        pins = _pin_version(pins, name, old, new)
    updates["Cargo.lock"] = lock
    updates[_PINS] = pins
    return updates


def _version_tree(repo: Path, candidate: str, updates: Mapping[str, str]) -> str:
    with tempfile.TemporaryDirectory(prefix="gobby-landing-index-") as temporary:
        env = {**(git_subprocess_env() or os.environ), "GIT_INDEX_FILE": f"{temporary}/index"}
        _git(repo, "read-tree", candidate, env=env)
        for path, content in sorted(updates.items()):
            blob = _git(repo, "hash-object", "-w", "--stdin", content=content, env=env)
            _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        return _git(repo, "write-tree", env=env)


def generate_version_bump(
    repo: Path, base: str, candidate: str, paths: Collection[str], *, task_ref: str
) -> str | None:
    """Return a generated version-only commit, or None when no binary changes."""
    try:
        updates = _updates(repo, base, candidate, paths)
        if not updates:
            return None
        tree = _version_tree(repo, candidate, updates)
        message = (
            f"[gobby-{task_ref}] chore: bump integrated binary versions\n\n"
            f"Gobby-Version-Base: {base}\nGobby-Version-Candidate: {candidate}\n"
        )
        return _git(repo, "commit-tree", tree, "-p", candidate, "-m", message)
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        SyntaxError,
    ) as exc:
        raise release.BinaryReleaseVersionError(
            "binary version inputs could not be processed"
        ) from exc


def verify_version_bump(repo: Path, base: str, candidate: str, bump: str) -> bool:
    """Verify the exact transformation against the admitted integration base."""
    if not all(_FULL_SHA.fullmatch(value) for value in (base, candidate, bump)):
        return False
    try:
        if _git(repo, "rev-list", "--parents", "-n", "1", bump).split() != [bump, candidate]:
            return False
        message = _git(repo, "show", "-s", "--format=%B", bump)
        if message.splitlines().count(f"Gobby-Version-Base: {base}") != 1 or (
            message.splitlines().count(f"Gobby-Version-Candidate: {candidate}") != 1
        ):
            return False
        if _git(repo, "merge-base", base, candidate) != base:
            return False
        paths = _git(repo, "diff", "--name-only", base, candidate).splitlines()
        updates = _updates(repo, base, candidate, paths)
        return bool(updates) and _version_tree(repo, candidate, updates) == _git(
            repo, "rev-parse", f"{bump}^{{tree}}"
        )
    except (
        release.BinaryReleaseVersionError,
        subprocess.SubprocessError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        SyntaxError,
    ):
        return False


def verify_landing_transform(
    repo: Path, *, reviewed: str, base: str, source: str, bump: str
) -> bool:
    """Bind the transformation to the reviewed candidate and exact integration base."""
    if not all(_FULL_SHA.fullmatch(value) for value in (reviewed, base, source, bump)):
        return False
    try:
        if source != reviewed:
            if _git(repo, "rev-list", "--parents", "-n", "1", source).split() != [
                source,
                base,
                reviewed,
            ]:
                return False
            merged = _git(repo, "merge-tree", "--write-tree", base, reviewed).splitlines()[0]
            if merged != _git(repo, "rev-parse", f"{source}^{{tree}}"):
                return False
        return verify_version_bump(repo, base, source, bump)
    except (
        release.BinaryReleaseVersionError,
        subprocess.SubprocessError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        SyntaxError,
    ):
        return False
