"""Bounded evidence collection for project verification refresh."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

STANDARD_SLOTS: tuple[str, ...] = (
    "unit_tests",
    "type_check",
    "lint",
    "format",
    "integration",
    "security",
    "code_review",
    "build",
    "doc_tests",
)

FRONTEND_SUBDIRS: tuple[str, ...] = (
    "web",
    "frontend",
    "client",
    "app",
    "ui",
    "packages/web",
    "packages/frontend",
)

MAX_FILE_BYTES = 64 * 1024
MAX_DOC_COMMANDS = 80
MAX_CI_COMMANDS = 120

COMMAND_LINE_RE = re.compile(
    r"\b("
    r"uv run|pytest|ruff|mypy|cargo|nextest|clippy|go test|go vet|go build|"
    r"npm|pnpm|yarn|vitest|tsc|eslint|bandit|uv build|make|just|task"
    r")\b"
)
DOC_COMMAND_START_RE = re.compile(
    r"^(?:[A-Z0-9_]+=\S+\s+)*(?:"
    r"uv run|pytest|ruff|mypy|cargo|nextest|go test|go vet|go build|"
    r"npm|pnpm|yarn|npx|vitest|tsc|eslint|bandit|uv build|make|just|task"
    r")\b"
)
INLINE_CODE_RE = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class EvidenceItem:
    """One compact evidence item for a verification command."""

    source: str
    kind: str
    command: str | None = None
    slot: str | None = None
    name: str | None = None
    text: str | None = None
    confidence: float = 0.0

    def to_prompt_dict(self) -> dict[str, Any]:
        """Return LLM-safe evidence payload."""
        payload: dict[str, Any] = {
            "source": self.source,
            "kind": self.kind,
        }
        if self.command:
            payload["command"] = self.command
        if self.slot:
            payload["slot"] = self.slot
        if self.name:
            payload["name"] = self.name
        if self.text:
            payload["text"] = self.text
        if self.confidence:
            payload["confidence"] = self.confidence
        return payload


@dataclass(frozen=True)
class PythonEvidence:
    """Parsed Python project evidence."""

    path: str
    has_src: bool
    has_tests: bool
    has_build_system: bool
    has_pytest_config: bool
    has_ruff_config: bool
    has_mypy_config: bool
    mypy_strict: bool


@dataclass(frozen=True)
class PackageScripts:
    """Parsed package.json scripts for one package root."""

    path: str
    subdir: str
    scripts: dict[str, str]


@dataclass
class EvidenceBundle:
    """All bounded evidence gathered from a project root."""

    root: Path
    existing_verification: dict[str, Any] = field(default_factory=dict)
    items: list[EvidenceItem] = field(default_factory=list)
    python: PythonEvidence | None = None
    packages: list[PackageScripts] = field(default_factory=list)
    has_cargo: bool = False
    has_nextest: bool = False
    has_go_mod: bool = False

    @property
    def project_json_path(self) -> Path:
        """Return the local project metadata path."""
        return self.root / ".gobby" / "project.json"

    def to_prompt_dict(self) -> dict[str, Any]:
        """Return compact evidence for synthesis."""
        return {
            "project_root_name": self.root.name,
            "existing_verification": self.existing_verification,
            "items": [item.to_prompt_dict() for item in self.items],
            "manifests": {
                "python": asdict(self.python) if self.python else None,
                "packages": [asdict(pkg) for pkg in self.packages],
                "cargo": self.has_cargo,
                "nextest": self.has_nextest,
                "go": self.has_go_mod,
            },
        }


def collect_evidence(root: Path) -> EvidenceBundle:
    """Collect deterministic, bounded evidence without running project commands."""
    root = root.resolve()
    bundle = EvidenceBundle(root=root)
    _collect_existing_project_json(bundle)
    _collect_pyproject(bundle)
    _collect_package_jsons(bundle)
    _collect_language_manifests(bundle)
    _collect_ci_commands(bundle)
    _collect_make_like_commands(bundle)
    _collect_doc_commands(bundle)
    return bundle


def _collect_existing_project_json(bundle: EvidenceBundle) -> None:
    project_file = bundle.project_json_path
    if not project_file.exists():
        return
    try:
        data = json.loads(_read_text(project_file))
    except (json.JSONDecodeError, OSError):
        return
    verification = data.get("verification")
    if not isinstance(verification, dict):
        return
    bundle.existing_verification = verification
    for name, command in verification.items():
        if name == "custom" and isinstance(command, dict):
            for custom_name, custom_command in command.items():
                if isinstance(custom_command, str):
                    bundle.items.append(
                        EvidenceItem(
                            source=".gobby/project.json",
                            kind="existing",
                            command=custom_command,
                            name=str(custom_name),
                            confidence=0.75,
                        )
                    )
            continue
        if name in STANDARD_SLOTS and isinstance(command, str):
            bundle.items.append(
                EvidenceItem(
                    source=".gobby/project.json",
                    kind="existing",
                    command=command,
                    slot=name,
                    name=name,
                    confidence=0.75,
                )
            )


def _collect_pyproject(bundle: EvidenceBundle) -> None:
    path = bundle.root / "pyproject.toml"
    if not path.exists():
        return
    try:
        data = tomllib.loads(_read_text(path))
    except (tomllib.TOMLDecodeError, OSError):
        return
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        tool = {}
    mypy = tool.get("mypy", {})
    pytest_cfg = tool.get("pytest")
    bundle.python = PythonEvidence(
        path="pyproject.toml",
        has_src=(bundle.root / "src").is_dir(),
        has_tests=(bundle.root / "tests").is_dir(),
        has_build_system=isinstance(data.get("build-system"), dict),
        has_pytest_config=isinstance(pytest_cfg, dict),
        has_ruff_config=isinstance(tool.get("ruff"), dict),
        has_mypy_config=isinstance(mypy, dict),
        mypy_strict=isinstance(mypy, dict) and mypy.get("strict") is True,
    )
    bundle.items.append(
        EvidenceItem(
            source="pyproject.toml",
            kind="manifest",
            text="Python project manifest",
            confidence=0.55,
        )
    )


def _collect_package_jsons(bundle: EvidenceBundle) -> None:
    for package_dir, subdir in _find_frontend_dirs(bundle.root):
        path = package_dir / "package.json"
        try:
            data = json.loads(_read_text(path))
        except (json.JSONDecodeError, OSError):
            continue
        scripts = data.get("scripts", {})
        if not isinstance(scripts, dict):
            continue
        normalized = {str(key): str(value) for key, value in scripts.items()}
        rel_path = "package.json" if subdir == "." else f"{subdir}/package.json"
        bundle.packages.append(PackageScripts(path=rel_path, subdir=subdir, scripts=normalized))
        for script_name, script_body in normalized.items():
            if _script_name_is_relevant(script_name):
                bundle.items.append(
                    EvidenceItem(
                        source=rel_path,
                        kind="package_script",
                        name=script_name,
                        text=script_body[:500],
                        confidence=0.65,
                    )
                )


def _collect_language_manifests(bundle: EvidenceBundle) -> None:
    bundle.has_cargo = (bundle.root / "Cargo.toml").exists()
    bundle.has_go_mod = (bundle.root / "go.mod").exists()
    bundle.has_nextest = any(
        (bundle.root / path).exists()
        for path in (
            ".config/nextest.toml",
            "nextest.toml",
            "Cargo.nextest.toml",
        )
    )
    if bundle.has_cargo:
        bundle.items.append(
            EvidenceItem(source="Cargo.toml", kind="manifest", text="Rust Cargo project")
        )
    if bundle.has_nextest:
        bundle.items.append(
            EvidenceItem(source="nextest config", kind="manifest", text="cargo-nextest config")
        )
    if bundle.has_go_mod:
        bundle.items.append(EvidenceItem(source="go.mod", kind="manifest", text="Go module"))


def _collect_ci_commands(bundle: EvidenceBundle) -> None:
    workflows_dir = bundle.root / ".github" / "workflows"
    if not workflows_dir.exists():
        return
    count = 0
    for path in sorted((*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml"))):
        if count >= MAX_CI_COMMANDS:
            return
        try:
            data = yaml.safe_load(_read_text(path)) or {}
        except (OSError, yaml.YAMLError):
            continue
        rel = _rel(bundle.root, path)
        for command, step_name in _workflow_commands(data):
            if count >= MAX_CI_COMMANDS:
                return
            if not _looks_like_command(command):
                continue
            bundle.items.append(
                EvidenceItem(
                    source=f"{rel}:{step_name}" if step_name else rel,
                    kind="ci",
                    command=command,
                    confidence=0.82,
                )
            )
            count += 1


def _collect_make_like_commands(bundle: EvidenceBundle) -> None:
    for filename in ("Makefile", "makefile", "justfile", "Justfile"):
        path = bundle.root / filename
        if not path.exists():
            continue
        for target, command in _parse_indented_recipes(_read_text(path)):
            if _script_name_is_relevant(target) and _looks_like_command(command):
                bundle.items.append(
                    EvidenceItem(
                        source=filename,
                        kind="recipe",
                        name=target,
                        command=command,
                        confidence=0.78,
                    )
                )

    for filename in ("Taskfile.yml", "Taskfile.yaml", "taskfile.yml", "taskfile.yaml"):
        path = bundle.root / filename
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(_read_text(path)) or {}
        except (OSError, yaml.YAMLError):
            continue
        tasks = data.get("tasks", {}) if isinstance(data, dict) else {}
        if not isinstance(tasks, dict):
            continue
        for name, task_data in tasks.items():
            task_command = _taskfile_command(task_data)
            if (
                task_command
                and _script_name_is_relevant(str(name))
                and _looks_like_command(task_command)
            ):
                bundle.items.append(
                    EvidenceItem(
                        source=filename,
                        kind="recipe",
                        name=str(name),
                        command=task_command,
                        confidence=0.78,
                    )
                )


def _collect_doc_commands(bundle: EvidenceBundle) -> None:
    count = 0
    for path in _doc_paths(bundle.root):
        if count >= MAX_DOC_COMMANDS:
            return
        try:
            text = _read_text(path)
        except OSError:
            continue
        for line in text.splitlines():
            if count >= MAX_DOC_COMMANDS:
                return
            command = _clean_doc_command_line(line)
            if not command or not _looks_like_command(command):
                continue
            bundle.items.append(
                EvidenceItem(
                    source=_rel(bundle.root, path),
                    kind="docs",
                    command=command,
                    confidence=0.68,
                )
            )
            count += 1


def _workflow_commands(data: Any) -> list[tuple[str, str]]:
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs", {})
    if not isinstance(jobs, dict):
        return []
    commands: list[tuple[str, str]] = []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            working_dir = step.get("working-directory")
            step_name = str(step.get("name") or f"step-{index + 1}")
            for command in _split_run_commands(run):
                if isinstance(working_dir, str) and not command.startswith("cd "):
                    command = f"cd {working_dir} && {command}"
                commands.append((command, step_name))
    return commands


def _split_run_commands(run: str) -> list[str]:
    commands: list[str] = []
    continued = ""
    awaiting_continuation = False
    rejected_trailing_backslash = False
    for raw_line in run.splitlines():
        line = raw_line.strip()
        if awaiting_continuation:
            line = f"{continued} {line}".strip()
            awaiting_continuation = False
        elif not line:
            continue

        trailing_backslashes = len(line) - len(line.rstrip("\\"))
        if trailing_backslashes % 2 == 1:
            continued = line[:-1].rstrip()
            awaiting_continuation = True
            continue
        if line.endswith("\\"):
            rejected_trailing_backslash = True
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("set ", "export ", "echo ")):
            continue
        commands.append(line)
    if awaiting_continuation or rejected_trailing_backslash:
        return commands
    if commands:
        return commands
    folded = run.strip()
    return [folded] if folded else []


def _parse_indented_recipes(text: str) -> list[tuple[str, str]]:
    recipes: list[tuple[str, str]] = []
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        target_match = re.match(r"^([A-Za-z0-9_.:-]+)\s*:(?:\s|$)", line)
        just_match = re.match(r"^([A-Za-z0-9_.:-]+)(?:\s+[^:=]+)?\s*$", line)
        if target_match and not line.startswith((" ", "\t")):
            current = target_match.group(1)
            continue
        if just_match and not line.startswith((" ", "\t")) and "=" not in line:
            current = just_match.group(1)
            continue
        if current and line.startswith((" ", "\t")):
            command = line.strip()
            if command and not command.startswith(("@", "-")):
                recipes.append((current, command))
            elif len(command) > 1:
                recipes.append((current, command[1:].strip()))
            current = None
    return recipes


def _taskfile_command(task_data: Any) -> str | None:
    if isinstance(task_data, str):
        return task_data
    if not isinstance(task_data, dict):
        return None
    cmds = task_data.get("cmds")
    if isinstance(cmds, list):
        for item in cmds:
            if isinstance(item, str):
                return item
            if isinstance(item, dict) and isinstance(item.get("cmd"), str):
                return str(item["cmd"])
    if isinstance(task_data.get("cmd"), str):
        return str(task_data["cmd"])
    return None


def _find_frontend_dirs(root: Path) -> list[tuple[Path, str]]:
    results: list[tuple[Path, str]] = []
    if (root / "package.json").exists():
        results.append((root, "."))
    for subdir in FRONTEND_SUBDIRS:
        candidate = root / subdir
        if (candidate / "package.json").exists():
            results.append((candidate, subdir))
    return results


def _doc_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for filename in ("AGENTS.md", "README.md", "CONTRIBUTING.md"):
        path = root / filename
        if path.exists():
            paths.append(path)
    for directory in (root / "docs" / "guides", root / "docs" / "architecture"):
        if directory.exists():
            paths.extend(sorted(directory.glob("*.md")))
    return paths[:40]


def _clean_doc_command_line(line: str) -> str | None:
    stripped = line.strip()
    for match in INLINE_CODE_RE.finditer(stripped):
        candidate = match.group(1).strip()
        if _looks_like_doc_command(candidate):
            return candidate

    stripped = stripped.removeprefix("-").strip()
    stripped = stripped.removeprefix("*").strip()
    stripped = re.sub(r"^\d+\.\s*", "", stripped)
    stripped = stripped.strip("` ")
    if "|" in stripped:
        parts = [part.strip().strip("` ") for part in stripped.split("|")]
        stripped = next((part for part in parts if _looks_like_doc_command(part)), stripped)
    return stripped if _looks_like_doc_command(stripped) else None


def _script_name_is_relevant(name: str) -> bool:
    lowered = name.lower()
    return any(
        token in lowered
        for token in (
            "test",
            "lint",
            "type",
            "tsc",
            "format",
            "fmt",
            "build",
            "doc",
            "security",
            "bandit",
            "vet",
            "clippy",
        )
    )


def _looks_like_command(command: str) -> bool:
    return bool(COMMAND_LINE_RE.search(command))


def _looks_like_doc_command(command: str) -> bool:
    return bool(DOC_COMMAND_START_RE.search(command))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
