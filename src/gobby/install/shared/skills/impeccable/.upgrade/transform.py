"""Re-vendor Impeccable from an identity-verified npm release channel artifact."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess  # Fixed npm and Node commands. # nosec B404
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from gobby.install.manifest import build_bundled_content_manifest

REPO_ROOT = Path(__file__).resolve().parents[7]
SKILL_RELATIVE = Path("src/gobby/install/shared/skills/impeccable")
INSTALL_RELATIVE = Path("src/gobby/install")
DEPENDENCIES_RELATIVE = Path("src/gobby/utils/dependency_requirements.py")

EXACT_SEMVER_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
NODE_ENGINE_RE = re.compile(r">=([0-9]+)\.([0-9]+)(?:\.([0-9]+))?\Z")
FRONTMATTER_VERSION_RE = re.compile(r"\A---\r?\n(?P<body>.*?)\r?\n---\r?\n", re.DOTALL)
REFERENCE_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n+", re.DOTALL)
CATALOGUE_ROW_RE = re.compile(r"^\| `(?P<path>references/[^`]+)` \| (?P<class>[^|]+) \|$")
ADDITIONAL_CONTEXT_RE = re.compile(
    r"\A> \*\*Additional context needed\*\*:\s*(?P<context>[^\n]+)\n?"
)
MARKDOWN_REFERENCE_RE = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<path>(?:reference/)?(?:degraded/)?[^)]+\.md)\)"
)
COMMAND_REFERENCE_RE = re.compile(r"/impeccable\s+(?P<name>[a-z][a-z0-9-]*)")
BACKTICK_COMMAND_REFERENCE_RE = re.compile(r"`/impeccable\s+(?P<name>[a-z][a-z0-9-]*)`")
PLACEHOLDER_RE = re.compile(r"\{\{[a-z_]+\}\}")

PREAMBLE = (
    "> You are continuing a session under the `impeccable` skill; "
    "the design-context protocol and anti-pattern rules already apply."
)
SCRIPTS_RESOLVER = (
    'Resolve `<scripts_dir>` by calling `materialize_skill_scripts(name="impeccable")` '
    "on `gobby-skills`; it returns the absolute path of the skill's materialized "
    "`scripts/` directory. Export the returned `environment.PUPPETEER_CACHE_DIR` "
    "before any browser-engine invocation. If the tool or Node is unavailable, "
    "skip detector runs and scan manually."
)
AVAILABLE_COMMANDS = (
    "`adapt`, `animate`, `audit`, `bolder`, `clarify`, `colorize`, `critique`, "
    "`delight`, `distill`, `harden`, `layout`, `optimize`, `overdrive`, `polish`, "
    "`quieter`, `shape`, `typeset`"
)

# These files carry Gobby prose beyond the standard mechanical adaptation even
# though their catalogue classification predates the explicit curated label.
CURATED_REFERENCE_PATHS = frozenset(
    {
        "references/adapt.native.md",
        "references/audit.native.md",
        "references/doctor.md",
        "references/visualize.md",
    }
)


class UpgradeRejected(RuntimeError):
    """A preflight authority could not produce a coherent vendored release."""


@dataclass(frozen=True)
class StagedCandidate:
    """Identity and generated skill roots produced inside a temporary workspace."""

    package_dir: Path
    skill_dir: Path


@dataclass(frozen=True)
class UpgradeReport:
    """Applied paths and manual judgments emitted by one upgrade run."""

    changed_paths: tuple[str, ...]
    judgment_needed: tuple[str, ...]

    def render(self) -> str:
        lines = [f"updated {path}" for path in self.changed_paths]
        if self.judgment_needed:
            lines.append("\nJUDGMENT NEEDED")
            lines.extend(self.judgment_needed)
        if not lines:
            return "release already reproduced byte-for-byte"
        return "\n".join(lines)


@dataclass(frozen=True)
class _PackageAuthority:
    version: str
    engine_floor: str
    dependencies: dict[str, str]
    optional_dependencies: dict[str, str]


@dataclass(frozen=True)
class _PreparedUpgrade:
    scripts_dir: Path
    writes: Mapping[Path, bytes]
    changed_paths: tuple[str, ...]
    judgment_needed: tuple[str, ...]


StageProvider = Callable[[str, Path], StagedCandidate]
LockfileBuilder = Callable[[bytes, Path], bytes]


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _package_json(value: object) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpgradeRejected(f"{label} is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise UpgradeRejected(f"{label} must be a JSON object")
    return value


def _string_map(value: object, label: str, *, required: bool) -> dict[str, str]:
    if value is None and not required:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(pin, str) and key and pin for key, pin in value.items()
    ):
        raise UpgradeRejected(f"{label} must contain non-empty string package pins")
    if required and not value:
        raise UpgradeRejected(f"{label} must not be empty")
    return dict(value)


def _normalize_node_engine(value: object) -> str:
    if not isinstance(value, str):
        raise UpgradeRejected("staged package engines.node is absent")
    match = NODE_ENGINE_RE.fullmatch(value)
    if match is None:
        raise UpgradeRejected(
            "staged package engines.node must be one inclusive >=MAJOR.MINOR[.PATCH] bound"
        )
    major, minor, patch = match.groups()
    return f"{int(major)}.{int(minor)}.{int(patch or '0')}"


def _validate_candidate_version(candidate_version: str) -> None:
    if EXACT_SEMVER_RE.fullmatch(candidate_version) is None:
        raise UpgradeRejected("candidate version must be exact MAJOR.MINOR.PATCH semver")


def _extract_skill_release(skill_file: Path) -> str:
    try:
        source = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UpgradeRejected(f"staged SKILL.md is unreadable: {exc}") from exc
    frontmatter = FRONTMATTER_VERSION_RE.match(source)
    if frontmatter is None:
        raise UpgradeRejected("staged SKILL.md has no YAML frontmatter")
    versions = re.findall(
        r'^version:\s*["\']?([^"\'\s]+)["\']?\s*$',
        frontmatter.group("body"),
        re.MULTILINE,
    )
    if len(versions) != 1 or EXACT_SEMVER_RE.fullmatch(versions[0]) is None:
        raise UpgradeRejected("staged SKILL.md must declare one exact release version")
    return cast(str, versions[0])


def _validate_tree(root: Path, label: str) -> None:
    if not root.is_dir():
        raise UpgradeRejected(f"staged {label} directory is absent")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise UpgradeRejected(f"staged {label} contains symlink {path.relative_to(root)}")


def _validate_staged_candidate(
    staged: StagedCandidate, candidate_version: str
) -> tuple[_PackageAuthority, str]:
    package = _read_json_object(staged.package_dir / "package.json", "staged package.json")
    if package.get("name") != "impeccable":
        raise UpgradeRejected(
            f"staged package name is {package.get('name')!r}, expected 'impeccable'"
        )
    resolved_version = package.get("version")
    if resolved_version != candidate_version:
        raise UpgradeRejected(
            f"staged package resolved version {resolved_version}, expected {candidate_version}"
        )
    engines = package.get("engines")
    if not isinstance(engines, dict):
        raise UpgradeRejected("staged package engines.node is absent")
    engine_floor = _normalize_node_engine(engines.get("node"))
    dependencies = _string_map(
        package.get("dependencies"), "staged package dependencies", required=True
    )
    optional_dependencies = _string_map(
        package.get("optionalDependencies"),
        "staged package optionalDependencies",
        required=False,
    )
    _validate_tree(staged.skill_dir / "scripts", "generated scripts")
    _validate_tree(staged.skill_dir / "reference", "generated references")
    skill_release = _extract_skill_release(staged.skill_dir / "SKILL.md")
    return (
        _PackageAuthority(
            version=candidate_version,
            engine_floor=engine_floor,
            dependencies=dependencies,
            optional_dependencies=optional_dependencies,
        ),
        skill_release,
    )


def _run_checked(command: list[str], *, cwd: Path, label: str) -> None:
    try:
        subprocess.run(  # nosec B603 # fixed executable and arguments
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise UpgradeRejected(f"{label} failed: {detail}") from exc


def stage_candidate(candidate_version: str, workspace: Path) -> StagedCandidate:
    """Install one exact npm candidate and generate its Claude release artifact."""
    harness = workspace / "npm-harness"
    harness.mkdir()
    package_json = {
        "name": "gobby-impeccable-upgrade",
        "private": True,
        "dependencies": {"impeccable": candidate_version},
    }
    (harness / "package.json").write_bytes(_canonical_json(package_json))
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        raise UpgradeRejected("npm and Node are required to stage an Impeccable release")
    _run_checked(
        [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=harness,
        label="npm candidate install",
    )

    package_dir = harness / "node_modules" / "impeccable"
    package = _read_json_object(package_dir / "package.json", "staged package.json")
    bin_value = package.get("bin")
    if isinstance(bin_value, str):
        bin_relative = bin_value
    elif isinstance(bin_value, dict) and isinstance(bin_value.get("impeccable"), str):
        bin_relative = bin_value["impeccable"]
    else:
        raise UpgradeRejected("staged package has no impeccable CLI bin")
    cli_path = package_dir / bin_relative
    if not cli_path.is_file():
        raise UpgradeRejected("staged impeccable CLI bin is absent")

    release_root = workspace / "release"
    release_root.mkdir()
    (release_root / ".git").mkdir()
    _run_checked(
        [
            node,
            str(cli_path),
            "install",
            "-y",
            "--providers=claude",
            "--scope=project",
            "--no-hooks",
        ],
        cwd=release_root,
        label="release artifact generation",
    )
    return StagedCandidate(
        package_dir=package_dir,
        skill_dir=release_root / ".claude" / "skills" / "impeccable",
    )


def build_lockfile(package_json: bytes, workspace: Path) -> bytes:
    """Generate an npm lockfile from exact package metadata in a temp harness."""
    harness = Path(tempfile.mkdtemp(prefix="lock-", dir=workspace))
    (harness / "package.json").write_bytes(package_json)
    npm = shutil.which("npm")
    if npm is None:
        raise UpgradeRejected("npm is required to regenerate lockfiles")
    _run_checked(
        [npm, "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=harness,
        label="npm lockfile generation",
    )
    try:
        return (harness / "package-lock.json").read_bytes()
    except OSError as exc:
        raise UpgradeRejected(f"npm did not produce package-lock.json: {exc}") from exc


def _validate_lockfile(lockfile: bytes, package_json: bytes, label: str) -> None:
    try:
        lock = json.loads(lockfile)
        package = json.loads(package_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpgradeRejected(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("lockfileVersion") != 3:
        raise UpgradeRejected(f"{label} must use lockfileVersion 3")
    packages = lock.get("packages")
    root = packages.get("") if isinstance(packages, dict) else None
    if not isinstance(root, dict):
        raise UpgradeRejected(f"{label} has no root package")
    for field in ("name", "dependencies", "optionalDependencies"):
        expected = package.get(field)
        actual = root.get(field)
        if expected is None and actual is None:
            continue
        if expected != actual:
            raise UpgradeRejected(f"{label} root {field} does not match package.json")


def _parse_catalogue(notice: str) -> dict[str, str]:
    catalogue: dict[str, str] = {}
    for line in notice.splitlines():
        match = CATALOGUE_ROW_RE.fullmatch(line)
        if match is not None:
            catalogue[match.group("path")] = match.group("class").strip()
    if not catalogue:
        raise UpgradeRejected("NOTICE.md has no reference adaptation catalogue")
    return catalogue


def _reference_loader(path: str) -> str:
    target = path.removeprefix("reference/")
    if target.startswith("references/"):
        target = target.removeprefix("references/")
    label = Path(target).stem
    return (
        f'`{label}` by calling `get_skill_file(name="impeccable", '
        f'path="references/{target}")` on `gobby-skills`'
    )


def _transform_prose(chunk: str) -> str:
    chunk = chunk.replace("{{scripts_path}}", "<scripts_dir>")
    chunk = chunk.replace(".claude/skills/impeccable/scripts", "<scripts_dir>")
    chunk = chunk.replace("DESIGN.md", ".impeccable.md")
    chunk = chunk.replace("{{config_file}}", ".impeccable.md")
    chunk = chunk.replace("{{ask_instruction}}", "Ask the user")
    chunk = chunk.replace(
        "call the AskUserQuestion tool to clarify.",
        "ask the user directly to clarify what you cannot infer",
    )
    chunk = chunk.replace("STOP and ask the user directly", "ask the user directly")
    chunk = chunk.replace("{{available_commands}}", AVAILABLE_COMMANDS)

    seen: set[str] = set()

    def markdown_reference(match: re.Match[str]) -> str:
        path = match.group("path").removeprefix("reference/")
        label = Path(path).stem
        if path in seen:
            return f"`{label}`"
        seen.add(path)
        return _reference_loader(path)

    chunk = MARKDOWN_REFERENCE_RE.sub(markdown_reference, chunk)

    def command_reference(match: re.Match[str]) -> str:
        path = f"{match.group('name')}.md"
        if path in seen:
            return f"`{match.group('name')}`"
        seen.add(path)
        return (
            f'call `get_skill_file(name="impeccable", path="references/{path}")` '
            "on `gobby-skills` and follow it"
        )

    chunk = re.sub(
        r"hand off to " + BACKTICK_COMMAND_REFERENCE_RE.pattern,
        command_reference,
        chunk,
    )
    chunk = BACKTICK_COMMAND_REFERENCE_RE.sub(command_reference, chunk)
    chunk = COMMAND_REFERENCE_RE.sub(command_reference, chunk)
    chunk = re.sub(r"\{\{command_prefix\}\}([a-z][a-z0-9-]*)", r"\1", chunk)
    return chunk


def _add_resolver(text: str) -> str:
    if "<scripts_dir>" not in text or SCRIPTS_RESOLVER in text:
        return text
    paragraphs = text.split("\n\n")
    for index, paragraph in enumerate(paragraphs):
        if "<scripts_dir>" in paragraph:
            paragraphs.insert(index + 1, SCRIPTS_RESOLVER)
            break
    return "\n\n".join(paragraphs)


def mechanical_reference_transform(source: str) -> str:
    """Apply harness-neutral transforms to one generated release reference."""
    text = REFERENCE_FRONTMATTER_RE.sub("", source, count=1)
    context_match = ADDITIONAL_CONTEXT_RE.match(text)
    if context_match is not None:
        context = context_match.group("context").strip()
        text = f"{PREAMBLE} Additional context needed: {context}\n" + text[context_match.end() :]
    elif not text.startswith(PREAMBLE):
        text = f"{PREAMBLE}\n\n{text.lstrip()}"

    chunks = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    text = "".join(
        chunk if index % 2 else _transform_prose(chunk) for index, chunk in enumerate(chunks)
    )
    text = _add_resolver(text)
    leftovers = sorted(set(PLACEHOLDER_RE.findall(text)))
    if leftovers:
        raise UpgradeRejected(f"reference has unsupported placeholders: {', '.join(leftovers)}")
    return text.rstrip() + "\n"


def _reference_plan(
    staged_root: Path,
    destination_root: Path,
    catalogue: Mapping[str, str],
) -> tuple[dict[Path, bytes], list[str]]:
    staged_files = {
        f"references/{path.relative_to(staged_root).as_posix()}": path
        for path in staged_root.rglob("*")
        if path.is_file()
    }
    unknown = sorted(set(staged_files) - set(catalogue))
    if unknown:
        raise UpgradeRejected(
            "released references are missing NOTICE.md classifications: " + ", ".join(unknown)
        )

    writes: dict[Path, bytes] = {}
    judgments: list[str] = []
    for relative, classification in sorted(catalogue.items()):
        destination = destination_root / relative.removeprefix("references/")
        source_path = staged_files.get(relative)
        if "Gobby-retained" in classification:
            if not destination.is_file():
                raise UpgradeRejected(f"retained reference is absent: {relative}")
            continue
        if source_path is None:
            raise UpgradeRejected(f"catalogued released reference is absent: {relative}")
        source = source_path.read_text(encoding="utf-8")
        if classification == "Vendored as-is":
            writes[destination] = source.encode()
            continue

        candidate = mechanical_reference_transform(source)
        mechanically_managed = (
            classification in {"Standard adaptation", "Near-verbatim native reference"}
            and relative not in CURATED_REFERENCE_PATHS
        )
        if mechanically_managed:
            writes[destination] = candidate.encode()
            continue
        if not destination.is_file():
            raise UpgradeRejected(f"curated reference is absent: {relative}")
        current = destination.read_text(encoding="utf-8")
        if current != candidate:
            diff = "\n".join(
                difflib.unified_diff(
                    candidate.splitlines(),
                    current.splitlines(),
                    fromfile=f"released/{relative}",
                    tofile=f"gobby/{relative}",
                    lineterm="",
                )
            )
            judgments.append(f"{relative} ({classification})\n{diff}")
    return writes, judgments


def _update_skill_runtime(
    source: str, *, candidate_version: str, skill_release: str, engine_floor: str
) -> str:
    lines = source.splitlines(keepends=True)
    runtime_index = next((i for i, line in enumerate(lines) if line.strip() == "runtime:"), None)
    if runtime_index is None:
        raise UpgradeRejected("SKILL.md has no metadata.gobby.runtime block")
    runtime_indent = len(lines[runtime_index]) - len(lines[runtime_index].lstrip())
    runtime_end = next(
        (
            i
            for i in range(runtime_index + 1, len(lines))
            if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= runtime_indent
        ),
        len(lines),
    )

    def replace_field(field: str, value: str, start: int, end: int) -> int:
        matches = [i for i in range(start, end) if lines[i].lstrip().startswith(f"{field}:")]
        if len(matches) != 1:
            raise UpgradeRejected(f"SKILL.md runtime must contain one {field} field")
        index = matches[0]
        indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
        lines[index] = f'{indent}{field}: "{value}"\n'
        return index

    replace_field("node", f">={engine_floor}", runtime_index + 1, runtime_end)
    skill_release_index = replace_field(
        "skill_release", skill_release, runtime_index + 1, runtime_end
    )
    cli_index = next(
        (i for i in range(runtime_index + 1, skill_release_index) if lines[i].strip() == "cli:"),
        None,
    )
    if cli_index is None:
        raise UpgradeRejected("SKILL.md runtime has no cli block")
    cli_indent = len(lines[cli_index]) - len(lines[cli_index].lstrip())
    cli_end = next(
        (
            i
            for i in range(cli_index + 1, runtime_end)
            if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= cli_indent
        ),
        runtime_end,
    )
    replace_field("version", candidate_version, cli_index + 1, cli_end)
    return "".join(lines)


def _replace_once(source: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, source, count=1, flags=re.DOTALL | re.MULTILINE)
    if count != 1:
        raise UpgradeRejected(f"could not locate one {label}")
    return updated


def _update_dependency_pins(
    source: str, *, candidate_version: str, engine_floor: str, lockfile_sha256: str
) -> str:
    source = _replace_once(
        source,
        r'(IMPECCABLE_RELEASE\s*=\s*ImpeccableRelease\(.*?\bversion\s*=\s*")[^"]+("\s*,)',
        rf"\g<1>{candidate_version}\g<2>",
        "IMPECCABLE_RELEASE.version",
    )
    source = _replace_once(
        source,
        r'(IMPECCABLE_RELEASE\s*=\s*ImpeccableRelease\(.*?\blockfile_sha256\s*=\s*")[^"]+("\s*,)',
        rf"\g<1>{lockfile_sha256}\g<2>",
        "IMPECCABLE_RELEASE.lockfile_sha256",
    )
    return _replace_once(
        source,
        r'(^IMPECCABLE_NODE_MIN_VERSION\s*=\s*")[^"]+("\s*$)',
        rf"\g<1>{engine_floor}\g<2>",
        "IMPECCABLE_NODE_MIN_VERSION",
    )


def _update_notice(
    source: str, *, candidate_version: str, skill_release: str, script_count: int
) -> str:
    source = _replace_once(
        source,
        r"(^- Script release: )[0-9]+\.[0-9]+\.[0-9]+",
        rf"\g<1>{skill_release}",
        "NOTICE.md script release",
    )
    source = _replace_once(
        source,
        r"`impeccable@[0-9]+\.[0-9]+\.[0-9]+` CLI",
        f"`impeccable@{candidate_version}` CLI",
        "NOTICE.md CLI provenance",
    )
    source = re.sub(
        r"(vendors all \d+ released\nreference files from the generated )[0-9]+\.[0-9]+\.[0-9]+( output)",
        rf"\g<1>{skill_release}\g<2>",
        source,
    )
    source = re.sub(
        r"(full released script tree under `scripts/` — taken from the \*\*generated\*\*\n  )[0-9]+\.[0-9]+\.[0-9]+( skill output)",
        rf"\g<1>{skill_release}\g<2>",
        source,
    )
    source = re.sub(
        r"(`scripts/` contains all )\d+( files copied \*\*unmodified\*\* from the generated\n)[0-9]+\.[0-9]+\.[0-9]+( release output)",
        rf"\g<1>{script_count}\g<2>{skill_release}\g<3>",
        source,
    )
    source = re.sub(
        r"(They are excluded from the )\d+(-file upstream count)",
        rf"\g<1>{script_count}\g<2>",
        source,
    )
    return source


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _prepare_upgrade(
    repo_root: Path,
    staged: StagedCandidate,
    authority: _PackageAuthority,
    skill_release: str,
    workspace: Path,
    lockfile_builder: LockfileBuilder,
) -> _PreparedUpgrade:
    install_dir = repo_root / INSTALL_RELATIVE
    shared_dir = install_dir / "shared"
    skill_dir = repo_root / SKILL_RELATIVE
    scripts_dir = skill_dir / "scripts"
    references_dir = skill_dir / "references"
    dependencies_path = repo_root / DEPENDENCIES_RELATIVE
    for required in (shared_dir, skill_dir, scripts_dir, references_dir, dependencies_path):
        if not required.exists():
            raise UpgradeRejected(f"destination path is absent: {required.relative_to(repo_root)}")

    scripts_package = _read_json_object(scripts_dir / "package.json", "scripts/package.json")
    scripts_package["version"] = skill_release
    scripts_package["dependencies"] = authority.dependencies
    scripts_package["optionalDependencies"] = authority.optional_dependencies
    scripts_package_bytes = _package_json(scripts_package)

    prepared_scripts = workspace / "prepared-scripts"
    shutil.copytree(staged.skill_dir / "scripts", prepared_scripts)
    (prepared_scripts / "package.json").write_bytes(scripts_package_bytes)
    try:
        scripts_lock = lockfile_builder(scripts_package_bytes, workspace)
    except UpgradeRejected:
        raise
    except Exception as exc:
        raise UpgradeRejected(f"scripts lockfile generation failed: {exc}") from exc
    _validate_lockfile(scripts_lock, scripts_package_bytes, "scripts/package-lock.json")
    (prepared_scripts / "package-lock.json").write_bytes(scripts_lock)

    managed_package = {
        "name": "gobby-managed-impeccable",
        "private": True,
        "dependencies": {"impeccable": authority.version},
    }
    managed_package_bytes = _package_json(managed_package)
    try:
        managed_lock = lockfile_builder(managed_package_bytes, workspace)
    except UpgradeRejected:
        raise
    except Exception as exc:
        raise UpgradeRejected(f"managed CLI lockfile generation failed: {exc}") from exc
    _validate_lockfile(managed_lock, managed_package_bytes, "impeccable-package-lock.json")

    notice_path = skill_dir / "NOTICE.md"
    notice_source = notice_path.read_text(encoding="utf-8")
    catalogue = _parse_catalogue(notice_source)
    reference_writes, judgments = _reference_plan(
        staged.skill_dir / "reference", references_dir, catalogue
    )

    skill_path = skill_dir / "SKILL.md"
    skill_source = skill_path.read_text(encoding="utf-8")
    old_node_match = re.search(r'^\s+node:\s*">=([^"\n]+)"', skill_source, re.MULTILINE)
    if old_node_match is None:
        raise UpgradeRejected("SKILL.md runtime node floor is absent")
    old_engine_floor = old_node_match.group(1)
    if old_engine_floor != authority.engine_floor:
        judgments.append(
            "Node engine floor changed "
            f"{old_engine_floor} -> {authority.engine_floor}; review downstream literal witnesses"
        )
    skill_bytes = _update_skill_runtime(
        skill_source,
        candidate_version=authority.version,
        skill_release=skill_release,
        engine_floor=authority.engine_floor,
    ).encode()
    notice_bytes = _update_notice(
        notice_source,
        candidate_version=authority.version,
        skill_release=skill_release,
        script_count=len(
            [path for path in (staged.skill_dir / "scripts").rglob("*") if path.is_file()]
        ),
    ).encode()
    dependencies_bytes = _update_dependency_pins(
        dependencies_path.read_text(encoding="utf-8"),
        candidate_version=authority.version,
        engine_floor=authority.engine_floor,
        lockfile_sha256=hashlib.sha256(managed_lock).hexdigest(),
    ).encode()

    writes: dict[Path, bytes] = {
        skill_path: skill_bytes,
        notice_path: notice_bytes,
        dependencies_path: dependencies_bytes,
        install_dir / "impeccable-package-lock.json": managed_lock,
        **reference_writes,
    }

    mirror = workspace / "shared"
    shutil.copytree(shared_dir, mirror)
    mirror_skill = mirror / "skills" / "impeccable"
    shutil.rmtree(mirror_skill / "scripts")
    shutil.copytree(prepared_scripts, mirror_skill / "scripts")
    for destination, content in writes.items():
        try:
            relative = destination.relative_to(shared_dir)
        except ValueError:
            continue
        mirror_destination = mirror / relative
        mirror_destination.parent.mkdir(parents=True, exist_ok=True)
        mirror_destination.write_bytes(content)
    manifest_bytes = _canonical_json(build_bundled_content_manifest(mirror))
    writes[install_dir / "bundled_content_manifest.json"] = manifest_bytes

    changed = [
        path.relative_to(repo_root).as_posix()
        for path, content in writes.items()
        if not path.is_file() or path.read_bytes() != content
    ]
    if _tree_bytes(scripts_dir) != _tree_bytes(prepared_scripts):
        current = _tree_bytes(scripts_dir)
        desired = _tree_bytes(prepared_scripts)
        changed.extend(
            (scripts_dir / relative).relative_to(repo_root).as_posix()
            for relative in sorted(set(current) | set(desired))
            if current.get(relative) != desired.get(relative)
        )
    return _PreparedUpgrade(
        scripts_dir=prepared_scripts,
        writes=writes,
        changed_paths=tuple(sorted(set(changed))),
        judgment_needed=tuple(judgments),
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_scripts(destination: Path, prepared: Path) -> None:
    if _tree_bytes(destination) == _tree_bytes(prepared):
        return
    token = uuid.uuid4().hex
    incoming = destination.parent / f".scripts-incoming-{token}"
    backup = destination.parent / f".scripts-backup-{token}"
    shutil.copytree(prepared, incoming)
    os.replace(destination, backup)
    try:
        os.replace(incoming, destination)
    except Exception:
        os.replace(backup, destination)
        raise
    shutil.rmtree(backup)


def _apply_upgrade(prepared: _PreparedUpgrade) -> None:
    destination_scripts = (
        next(
            path.parent
            for path in prepared.writes
            if path.name == "SKILL.md" and path.parent.name == "impeccable"
        )
        / "scripts"
    )
    _replace_scripts(destination_scripts, prepared.scripts_dir)
    for path, content in prepared.writes.items():
        if path.is_file() and path.read_bytes() == content:
            continue
        _atomic_write(path, content)


def upgrade(
    repo_root: Path,
    candidate_version: str,
    *,
    stage_provider: StageProvider = stage_candidate,
    lockfile_builder: LockfileBuilder = build_lockfile,
) -> UpgradeReport:
    """Preflight and atomically publish one exact Impeccable candidate."""
    _validate_candidate_version(candidate_version)
    with tempfile.TemporaryDirectory(prefix="gobby-impeccable-upgrade-") as temporary:
        workspace = Path(temporary)
        try:
            staged = stage_provider(candidate_version, workspace)
        except UpgradeRejected:
            raise
        except Exception as exc:
            raise UpgradeRejected(f"candidate staging failed: {exc}") from exc
        authority, skill_release = _validate_staged_candidate(staged, candidate_version)
        prepared = _prepare_upgrade(
            repo_root,
            staged,
            authority,
            skill_release,
            workspace,
            lockfile_builder,
        )
        _apply_upgrade(prepared)
        return UpgradeReport(
            changed_paths=prepared.changed_paths,
            judgment_needed=prepared.judgment_needed,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate_version", help="exact npm CLI version (MAJOR.MINOR.PATCH)")
    args = parser.parse_args(argv)
    try:
        report = upgrade(REPO_ROOT, args.candidate_version)
    except UpgradeRejected as exc:
        print(f"JUDGMENT NEEDED: upgrade rejected: {exc}", file=sys.stderr)
        return 2
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
