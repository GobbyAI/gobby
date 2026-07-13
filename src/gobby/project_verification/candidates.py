"""Deterministic verification command candidates."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from gobby.project_verification.evidence import STANDARD_SLOTS, EvidenceBundle, EvidenceItem

GENERIC_EXISTING_COMMANDS = {
    "cargo test",
    "cargo clippy",
    "go test ./...",
    "go vet ./...",
    "npm test",
    "pytest",
    "uv run pytest",
}

SHELL_CONTROL_CHARACTERS = frozenset(";&|<>")
VALIDATION_EXECUTABLES = frozenset(
    {
        "bandit",
        "bun",
        "cargo",
        "deno",
        "eslint",
        "go",
        "hatch",
        "just",
        "make",
        "mypy",
        "nox",
        "npm",
        "npx",
        "pdm",
        "pipenv",
        "pnpm",
        "poetry",
        "prettier",
        "pytest",
        "python",
        "python3",
        "ruff",
        "safety",
        "semgrep",
        "task",
        "tox",
        "tsc",
        "uv",
        "vitest",
        "yarn",
    }
)

SOURCE_RANK = {
    "ai": 7,
    "existing": 6,
    "ci": 5,
    "recipe": 4,
    "package_script": 3,
    "manifest": 2,
    "docs": 1,
}


@dataclass(frozen=True)
class CommandCandidate:
    """One proposed verification command."""

    name: str
    slot: str
    command: str
    confidence: float
    source: str
    source_kind: str
    rationale: str
    custom: bool = False

    def to_prompt_dict(self) -> dict[str, Any]:
        """Return compact candidate payload for synthesis."""
        return {
            "name": self.name,
            "slot": self.slot,
            "command": self.command,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "source_kind": self.source_kind,
            "rationale": self.rationale,
            "custom": self.custom,
        }


def generate_candidates(bundle: EvidenceBundle) -> list[CommandCandidate]:
    """Generate deterministic candidates from collected evidence."""
    candidates: list[CommandCandidate] = []
    candidates.extend(_existing_candidates(bundle))

    primary_claimed = bool(bundle.python or bundle.has_cargo or bundle.has_go_mod)
    if bundle.python:
        candidates.extend(_python_candidates(bundle, custom=False))
    if bundle.has_cargo:
        candidates.extend(
            _rust_candidates(custom=primary_claimed and bool(bundle.python), bundle=bundle)
        )
        primary_claimed = True
    if bundle.has_go_mod:
        candidates.extend(_go_candidates(custom=primary_claimed, bundle=bundle))
        primary_claimed = True

    for package in bundle.packages:
        is_custom = primary_claimed
        candidates.extend(_package_candidates(package.subdir, package.scripts, custom=is_custom))
        if not is_custom and any(_script_slot(name) for name in package.scripts):
            primary_claimed = True

    for item in bundle.items:
        if item.command and item.kind in {"ci", "recipe", "docs"}:
            candidate = _candidate_from_command_item(item)
            if candidate:
                candidates.append(candidate)

    return [candidate for candidate in candidates if is_safe_validation_command(candidate.command)]


def select_best_candidates(candidates: list[CommandCandidate]) -> dict[str, CommandCandidate]:
    """Select the best evidenced command per output name."""
    selected: dict[str, CommandCandidate] = {}
    for candidate in candidates:
        current = selected.get(candidate.name)
        if current is None or _is_better(candidate, current):
            selected[candidate.name] = candidate
    return selected


def verification_dict_from_candidates(selected: dict[str, CommandCandidate]) -> dict[str, Any]:
    """Convert selected candidates to .gobby/project.json verification shape."""
    verification: dict[str, Any] = {}
    for slot in STANDARD_SLOTS:
        candidate = selected.get(slot)
        if candidate and not candidate.custom:
            verification[slot] = candidate.command

    custom: dict[str, str] = {}
    for name in sorted(selected):
        candidate = selected[name]
        if candidate.custom or name not in STANDARD_SLOTS:
            custom[name] = candidate.command
    if custom:
        verification["custom"] = custom
    return verification


def classify_command(command: str) -> str | None:
    """Classify a command into a verification slot."""
    lowered = command.lower()
    if "cargo test --doc" in lowered or "test --doc" in lowered:
        return "doc_tests"
    if "bandit" in lowered or "safety" in lowered or "semgrep" in lowered:
        return "security"
    if "clippy" in lowered or "ruff check" in lowered or "eslint" in lowered or "go vet" in lowered:
        return "lint"
    if "mypy" in lowered or "tsc" in lowered or "type-check" in lowered or "typecheck" in lowered:
        return "type_check"
    if _is_format_check(lowered):
        return "format"
    if (
        "pytest" in lowered
        or "cargo test" in lowered
        or "nextest" in lowered
        or "go test" in lowered
        or "npm test" in lowered
        or "vitest" in lowered
    ):
        return "unit_tests"
    if _is_build_command(lowered):
        return "build"
    return None


def is_safe_validation_command(command: str, slot: str | None = None) -> bool:
    """Reject mutating forms that are inappropriate for verification."""
    invocation = _validation_invocation(command)
    if not invocation:
        return False
    lowered = command.lower()
    tokens = _command_tokens(lowered)
    if _has_mutating_option(tokens):
        return False
    if _has_token_sequence(tokens, ("npm", "run", "format")):
        return False
    if _has_token_sequence(tokens, ("yarn", "format")):
        return False
    if "ruff format" in lowered and "--check" not in lowered:
        return False
    if "cargo fmt" in lowered and "--check" not in lowered:
        return False
    if "prettier" in lowered and "--check" not in lowered and slot == "format":
        return False
    return True


def _validation_invocation(command: str) -> list[str] | None:
    if any(marker in command for marker in ("`", "$(", "\n", "\r")):
        return None

    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens:
        return None

    contains_control_character = not SHELL_CONTROL_CHARACTERS.isdisjoint(command)
    control_positions = [
        index for index, token in enumerate(tokens) if _is_shell_control_token(token)
    ]
    if control_positions:
        if control_positions != [2] or tokens[:1] != ["cd"] or tokens[2] != "&&":
            return None
        if len(tokens) < 4 or not _is_safe_subdir(tokens[1]):
            return None
        tokens = tokens[3:]
    elif contains_control_character:
        return None

    while tokens and _is_environment_assignment(tokens[0]):
        tokens = tokens[1:]
    if not tokens or tokens[0].lower() not in VALIDATION_EXECUTABLES:
        return None
    return tokens


def _is_safe_subdir(subdir: str) -> bool:
    if not subdir or subdir.startswith(("/", "~", "-")):
        return False
    return ".." not in subdir.replace("\\", "/").split("/")


def _is_shell_control_token(token: str) -> bool:
    return bool(token) and set(token) <= SHELL_CONTROL_CHARACTERS


def _is_environment_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    return bool(separator and name and name.replace("_", "a").isalnum() and not name[0].isdigit())


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _has_mutating_option(tokens: list[str]) -> bool:
    return any(
        token in {"--fix", "--write"} or token.startswith(("--fix=", "--write="))
        for token in tokens
    )


def _has_token_sequence(tokens: list[str], unsafe_sequence: tuple[str, ...]) -> bool:
    length = len(unsafe_sequence)
    if length == 0 or len(tokens) < length:
        return False
    return any(
        tuple(tokens[index : index + length]) == unsafe_sequence
        for index in range(len(tokens) - length + 1)
    )


def command_evidence_key(name: str, command: str) -> tuple[str, str]:
    """Return a stable key for exact evidenced commands."""
    return name, " ".join(command.split())


def _existing_candidates(bundle: EvidenceBundle) -> list[CommandCandidate]:
    candidates: list[CommandCandidate] = []
    for name, value in bundle.existing_verification.items():
        if name == "custom" and isinstance(value, dict):
            for custom_name, command in value.items():
                if isinstance(command, str):
                    candidates.append(
                        CommandCandidate(
                            name=str(custom_name),
                            slot=classify_command(command) or "custom",
                            command=command,
                            confidence=_existing_confidence(command, custom=True),
                            source=".gobby/project.json",
                            source_kind="existing",
                            rationale="Existing custom verification command",
                            custom=True,
                        )
                    )
            continue
        if name in STANDARD_SLOTS and isinstance(value, str):
            candidates.append(
                CommandCandidate(
                    name=name,
                    slot=name,
                    command=value,
                    confidence=_existing_confidence(value, custom=False),
                    source=".gobby/project.json",
                    source_kind="existing",
                    rationale="Existing project verification command",
                )
            )
    return candidates


def _python_candidates(bundle: EvidenceBundle, *, custom: bool) -> list[CommandCandidate]:
    python = bundle.python
    if not python:
        return []
    target = "src/" if python.has_src else "."
    runner = {
        "uv": "uv run ",
        "poetry": "poetry run ",
        "pdm": "pdm run ",
    }.get(python.package_manager or "", "")
    test_prefix = (
        "GOBBY_TEST_PROTECT=1 "
        if python.project_name and python.project_name.strip().casefold() == "gobby"
        else ""
    )
    candidates: list[CommandCandidate] = []
    if python.has_tests or python.has_pytest_config:
        candidates.append(
            _candidate(
                "unit_tests",
                f"{test_prefix}{runner}pytest tests/ -v",
                0.58,
                "pyproject.toml",
                "manifest",
            )
        )
    type_command = f"{runner}mypy {target}"
    if python.mypy_strict:
        type_command = f"{type_command} --no-incremental --strict"
    candidates.append(_candidate("type_check", type_command, 0.68, "pyproject.toml", "manifest"))
    candidates.append(
        _candidate("lint", f"{runner}ruff check {target}", 0.64, "pyproject.toml", "manifest")
    )
    candidates.append(
        _candidate(
            "format", f"{runner}ruff format --check {target}", 0.64, "pyproject.toml", "manifest"
        )
    )
    if python.has_build_system:
        build_command = {
            "uv": "uv build",
            "poetry": "poetry build",
            "pdm": "pdm build",
        }.get(python.package_manager or "", "python -m build")
        candidates.append(_candidate("build", build_command, 0.56, "pyproject.toml", "manifest"))
    if custom:
        return [_as_custom(candidate, f"python_{candidate.name}") for candidate in candidates]
    return candidates


def _rust_candidates(bundle: EvidenceBundle, *, custom: bool) -> list[CommandCandidate]:
    candidates = [
        _candidate("build", "cargo build", 0.56, "Cargo.toml", "manifest"),
        _candidate("unit_tests", "cargo test", 0.56, "Cargo.toml", "manifest"),
        _candidate("doc_tests", "cargo test --doc", 0.56, "Cargo.toml", "manifest"),
        _candidate("format", "cargo fmt --check", 0.56, "Cargo.toml", "manifest"),
        _candidate("lint", "cargo clippy", 0.56, "Cargo.toml", "manifest"),
    ]
    if bundle.has_nextest:
        candidates.append(
            _candidate("unit_tests", "cargo nextest run", 0.74, "nextest config", "manifest")
        )
    if custom:
        names = {
            "unit_tests": "cargo_test",
            "lint": "clippy",
            "format": "cargo_fmt",
            "build": "cargo_build",
            "doc_tests": "cargo_doc_tests",
        }
        return [_as_custom(candidate, names[candidate.name]) for candidate in candidates]
    return candidates


def _go_candidates(bundle: EvidenceBundle, *, custom: bool) -> list[CommandCandidate]:
    candidates = [
        _candidate("unit_tests", "go test ./...", 0.58, "go.mod", "manifest"),
        _candidate("lint", "go vet ./...", 0.58, "go.mod", "manifest"),
        _candidate("build", "go build ./...", 0.55, "go.mod", "manifest"),
    ]
    if custom:
        names = {"unit_tests": "go_test", "lint": "go_vet", "build": "go_build"}
        return [_as_custom(candidate, names[candidate.name]) for candidate in candidates]
    return candidates


def _package_candidates(
    subdir: str,
    scripts: dict[str, str],
    *,
    custom: bool,
) -> list[CommandCandidate]:
    candidates: list[CommandCandidate] = []
    for script in _ordered_scripts(scripts):
        slot = _script_slot(script)
        if not slot:
            continue
        command = _package_script_command(subdir, script)
        if not is_safe_validation_command(command, slot):
            continue
        candidate = _candidate(slot, command, 0.66, _package_source(subdir), "package_script")
        candidates.append(
            _as_custom(candidate, _package_custom_name(subdir, slot)) if custom else candidate
        )
    return candidates


def _candidate_from_command_item(item: EvidenceItem) -> CommandCandidate | None:
    if not item.command:
        return None
    slot = classify_command(item.command)
    if not slot or not is_safe_validation_command(item.command, slot):
        return None
    custom = _is_frontend_command(item.command)
    name = _package_custom_name("web", slot) if custom else slot
    return CommandCandidate(
        name=name,
        slot=slot,
        command=item.command,
        confidence=item.confidence,
        source=item.source,
        source_kind=item.kind,
        rationale=f"Detected {slot} command in {item.kind} evidence",
        custom=custom,
    )


def _script_slot(script: str) -> str | None:
    lowered = script.lower().replace("_", "-")
    if lowered in {"test", "tests", "unit-test", "unit-tests", "vitest"}:
        return "unit_tests"
    if lowered in {"lint", "eslint", "clippy", "vet"} or lowered.startswith("lint:"):
        return "lint"
    if lowered in {"type-check", "typecheck", "types", "tsc"}:
        return "type_check"
    if lowered in {"format:check", "fmt:check", "check-format"}:
        return "format"
    if lowered in {"build", "compile"} or lowered.startswith("build:"):
        return "build"
    if lowered in {"doc-test", "doc-tests", "doctest", "doctests"}:
        return "doc_tests"
    if lowered in {"security", "bandit"}:
        return "security"
    return None


def _ordered_scripts(scripts: dict[str, str]) -> list[str]:
    priority = (
        "test",
        "lint",
        "type-check",
        "typecheck",
        "types",
        "tsc",
        "format:check",
        "fmt:check",
        "build",
        "doc-test",
        "doc-tests",
        "security",
    )
    ordered = [script for script in priority if script in scripts]
    ordered.extend(script for script in scripts if script not in ordered and _script_slot(script))
    return ordered


def _package_script_command(subdir: str, script: str) -> str:
    command = "npm test" if script == "test" else f"npm run {shlex.quote(script)}"
    return command if subdir == "." else f"cd {shlex.quote(subdir)} && {command}"


def _package_custom_name(subdir: str, slot: str) -> str:
    prefix = "frontend" if subdir != "." else "node"
    names = {
        "unit_tests": f"{prefix}_tests",
        "lint": f"{prefix}_lint",
        "type_check": "ts_check" if prefix == "frontend" else "node_type_check",
        "format": f"{prefix}_format",
        "build": f"{prefix}_build",
        "doc_tests": f"{prefix}_doc_tests",
        "security": f"{prefix}_security",
    }
    return names.get(slot, f"{prefix}_{slot}")


def _package_source(subdir: str) -> str:
    return "package.json" if subdir == "." else f"{subdir}/package.json"


def _candidate(
    slot: str,
    command: str,
    confidence: float,
    source: str,
    source_kind: str,
) -> CommandCandidate:
    return CommandCandidate(
        name=slot,
        slot=slot,
        command=command,
        confidence=confidence,
        source=source,
        source_kind=source_kind,
        rationale=f"Detected {slot} command from {source_kind} evidence",
    )


def _as_custom(candidate: CommandCandidate, name: str) -> CommandCandidate:
    return CommandCandidate(
        name=name,
        slot=candidate.slot,
        command=candidate.command,
        confidence=candidate.confidence,
        source=candidate.source,
        source_kind=candidate.source_kind,
        rationale=candidate.rationale,
        custom=True,
    )


def _existing_confidence(command: str, *, custom: bool) -> float:
    normalized = " ".join(command.split())
    if normalized in GENERIC_EXISTING_COMMANDS:
        return 0.62
    if any(token in normalized for token in ("--workspace", "--strict", "nextest", " --", "cd ")):
        return 0.9 if custom else 0.88
    if len(normalized.split()) >= 4:
        return 0.86 if custom else 0.84
    return 0.76 if custom else 0.74


def _is_better(candidate: CommandCandidate, current: CommandCandidate) -> bool:
    if candidate.confidence > current.confidence + 0.001:
        return True
    if abs(candidate.confidence - current.confidence) <= 0.001:
        return SOURCE_RANK.get(candidate.source_kind, 0) > SOURCE_RANK.get(current.source_kind, 0)
    return False


def _is_format_check(lowered: str) -> bool:
    return (
        ("ruff format" in lowered and "--check" in lowered)
        or ("cargo fmt" in lowered and "--check" in lowered)
        or ("prettier" in lowered and "--check" in lowered)
    )


def _is_build_command(lowered: str) -> bool:
    build_prefixes = (
        "uv build",
        "poetry build",
        "pdm build",
        "python -m build",
        "python3 -m build",
        "go build",
        "cargo build",
        "npm run build",
        "pnpm build",
        "yarn build",
        "make build",
        "just build",
        "task build",
    )
    return lowered.startswith(build_prefixes) or any(
        token in lowered
        for token in (
            "&& uv build",
            "&& poetry build",
            "&& pdm build",
            "&& python -m build",
            "&& go build",
            "&& cargo build",
            "&& npm run build",
            "&& pnpm build",
            "&& yarn build",
        )
    )


def _is_frontend_command(command: str) -> bool:
    lowered = command.lower()
    return lowered.startswith("cd web &&") or " npm " in f" {lowered} " or " npx " in f" {lowered} "
