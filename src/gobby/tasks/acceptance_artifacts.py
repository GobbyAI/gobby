"""Deterministic acceptance-artifact and provenance checks."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

_ARTIFACT_REF_RE = re.compile(
    r"\b(?P<kind>test|file):\s*(?:`(?P<quoted>[^`]+)`|(?P<bare>[^\s,;]+))",
    re.IGNORECASE,
)
_FIELD_RE = re.compile(
    r"^\s*-\s*(?P<key>workflow_name|run_url|commit_sha|utc_timestamp):\s*(?P<value>.+?)\s*$",
    re.MULTILINE,
)
_WORKFLOW_NAME_RE = re.compile(r"^\s*name:\s*['\"]?(?P<name>.+?)['\"]?\s*$", re.MULTILINE)
_ASSERTION_FAILURE_RE = re.compile(
    r"AssertionError|assertion failed|\bassert\b|panicked at|\bFAILED\b",
    re.IGNORECASE,
)
_NON_ASSERTION_FAILURE_RE = re.compile(
    r"ImportError|ModuleNotFoundError|collection error|error collecting|"
    r"failed to collect|configuration error|usage error",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AcceptanceTest:
    """One exact test reference and its gcode-resolved body."""

    reference: str
    path: str
    symbol: str
    body: str


@dataclass(frozen=True, slots=True)
class AcceptanceArtifactResult:
    """Outcome of deterministic acceptance-artifact checks."""

    passed: bool
    tests: tuple[AcceptanceTest, ...]
    findings: tuple[str, ...]
    evidence_files: tuple[str, ...]

    def details(self) -> dict[str, object]:
        return {
            "test_references": [test.reference for test in self.tests],
            "evidence_files": list(self.evidence_files),
            "findings": list(self.findings),
        }


def extract_artifact_references(criteria: str, kind: str) -> tuple[str, ...]:
    """Extract stable, deduplicated test or file references from criteria."""
    references: list[str] = []
    for match in _ARTIFACT_REF_RE.finditer(criteria):
        if match.group("kind").casefold() != kind.casefold():
            continue
        value = (match.group("quoted") or match.group("bare") or "").strip().rstrip(".")
        if value and value not in references:
            references.append(value)
    return tuple(references)


def evaluate_acceptance_artifacts(
    *,
    criteria: str,
    repo_path: str,
    commit_shas: list[str],
) -> AcceptanceArtifactResult:
    """Resolve named tests, reject placebo bodies, and verify local evidence provenance."""
    tests, resolution_findings = resolve_acceptance_tests(criteria, repo_path)
    findings = list(resolution_findings)
    for test in tests:
        findings.extend(_test_body_findings(test))

    evidence_files = extract_artifact_references(criteria, "file")
    findings.extend(
        validate_structured_file_evidence(
            evidence_files=evidence_files,
            repo_path=repo_path,
            commit_shas=commit_shas,
        )
    )
    return AcceptanceArtifactResult(
        passed=not findings,
        tests=tests,
        findings=tuple(findings),
        evidence_files=evidence_files,
    )


def resolve_acceptance_tests(
    criteria: str,
    repo_path: str,
) -> tuple[tuple[AcceptanceTest, ...], tuple[str, ...]]:
    """Resolve every named acceptance test through gcode."""
    tests: list[AcceptanceTest] = []
    findings: list[str] = []
    for reference in extract_artifact_references(criteria, "test"):
        parsed = _parse_test_reference(reference)
        if parsed is None:
            findings.append(f"{reference}: malformed test reference; expected path::test_symbol")
            continue
        path, symbol = parsed
        path_error = _path_error(path, repo_path)
        if path_error:
            findings.append(f"{reference}: {path_error}")
            continue
        try:
            body = _resolve_test_body(path, symbol, repo_path)
        except (OSError, RuntimeError, ValueError) as exc:
            findings.append(f"{reference}: gcode could not resolve the exact test body: {exc}")
            continue
        tests.append(AcceptanceTest(reference, path, symbol, body))
    return tuple(tests), tuple(findings)


def render_acceptance_test_bodies(tests: tuple[AcceptanceTest, ...]) -> str:
    """Render exact named test bodies for criteria-review evidence."""
    if not tests:
        return "Named acceptance tests: none."
    parts = ["Named acceptance tests (exact gcode-resolved bodies):"]
    for test in tests:
        parts.append(f"\n### {test.reference}\n{test.body}")
    return "\n".join(parts)


def is_assertion_failure(output: str | None) -> bool:
    """Return whether command output proves a test assertion or panic failure."""
    if not output or _NON_ASSERTION_FAILURE_RE.search(output):
        return False
    return _ASSERTION_FAILURE_RE.search(output) is not None


def validation_run_names_test(command: str, output: str | None, test: AcceptanceTest) -> bool:
    """Return whether a run identifies the exact acceptance test."""
    evidence = f"{command}\n{output or ''}"
    symbol_variants = (test.symbol, test.symbol.replace(".", "::"))
    return any(symbol in evidence for symbol in symbol_variants) and (
        test.path in evidence or Path(test.path).name in evidence
    )


def validation_run_covers_test(command: str, output: str | None, test: AcceptanceTest) -> bool:
    """Return whether a successful run covers the named test or its complete file."""
    evidence = f"{command}\n{output or ''}"
    return test.path in evidence or test.reference in evidence


def validate_structured_file_evidence(
    *,
    evidence_files: tuple[str, ...],
    repo_path: str,
    commit_shas: list[str],
) -> tuple[str, ...]:
    """Validate structured CI evidence using only repository-local facts."""
    findings: list[str] = []
    repo_slug = _repository_slug(repo_path)
    for path in evidence_files:
        path_error = _path_error(path, repo_path)
        if path_error:
            findings.append(f"{path}: {path_error}")
            continue
        try:
            content = _read_committed_file(path, commit_shas, repo_path)
        except RuntimeError as exc:
            findings.append(f"{path}: {exc}")
            continue
        blocks = _structured_run_blocks(content)
        for index, fields in enumerate(blocks, start=1):
            label = f"{path} run {index}"
            missing = sorted(
                {"workflow_name", "run_url", "commit_sha", "utc_timestamp"} - fields.keys()
            )
            if missing:
                findings.append(f"{label}: missing structured fields: {', '.join(missing)}")
                continue
            sha = fields["commit_sha"]
            timestamp = _parse_utc(fields["utc_timestamp"])
            if timestamp is None:
                findings.append(f"{label}: invalid utc_timestamp {fields['utc_timestamp']!r}")
                continue
            commit_time = _commit_time(sha, repo_path)
            if commit_time is None:
                findings.append(f"{label}: cited commit {sha} does not exist")
            elif commit_time > timestamp:
                findings.append(
                    f"{label}: cited commit {sha} is newer than the cited run timestamp"
                )
            if repo_slug is None:
                findings.append(
                    f"{label}: repository origin is unavailable for URL ownership proof"
                )
            elif not _is_repo_actions_url(fields["run_url"], repo_slug):
                findings.append(
                    f"{label}: run_url is not a repository-owned GitHub Actions run URL"
                )
            if commit_time is not None and not _workflow_exists(
                sha, fields["workflow_name"], repo_path
            ):
                findings.append(
                    f"{label}: producer workflow {fields['workflow_name']!r} "
                    f"is absent from cited commit {sha}"
                )
    return tuple(findings)


def _parse_test_reference(reference: str) -> tuple[str, str] | None:
    if "::" not in reference:
        return None
    path, symbol = reference.split("::", 1)
    path = path.strip()
    symbol = symbol.strip()
    return (path, symbol) if path and symbol else None


def _resolve_test_body(path: str, symbol: str, repo_path: str) -> str:
    query = symbol.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    search_raw = _run_command(
        [
            "gcode",
            "search-symbol",
            query,
            path,
            "--format",
            "json",
            "--limit",
            "20",
        ],
        repo_path,
    )
    try:
        rows = json.loads(search_raw).get("results", [])
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid gcode search response") from exc
    candidates = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("file_path") == path
        and (
            row.get("name") == query
            or str(row.get("qualified_name", "")).replace(".", "::").endswith(symbol)
        )
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"expected one matching symbol, found {len(candidates)}")
    symbol_id = candidates[0].get("id")
    if not isinstance(symbol_id, str):
        raise RuntimeError("gcode result omitted symbol id")
    symbol_raw = _run_command(["gcode", "symbol", symbol_id], repo_path)
    try:
        source = json.loads(symbol_raw).get("source")
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid gcode symbol response") from exc
    if not isinstance(source, str) or not source.strip():
        raise RuntimeError("gcode symbol response omitted source")
    return source


def _test_body_findings(test: AcceptanceTest) -> list[str]:
    suffix = Path(test.path).suffix.casefold()
    if suffix == ".py":
        return _python_test_findings(test)
    return _text_test_findings(test)


def _python_test_findings(test: AcceptanceTest) -> list[str]:
    try:
        tree = ast.parse(textwrap.dedent(test.body))
    except SyntaxError:
        return [f"{test.reference}: test body is not parseable Python"]
    findings: list[str] = []
    has_assertion = False
    current_name = test.symbol.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = _called_name(node.func)
            if called.startswith("test_") and called != current_name:
                findings.append(f"{test.reference}: delegates acceptance to another test {called}")
            if called in {"assertEqual", "assertNotEqual", "assertTrue", "assertFalse"} and all(
                _is_static_expression(arg) for arg in node.args
            ):
                findings.append(f"{test.reference}: assertion compares only constant values")
            if called.startswith("assert") or called in {"fail", "raises"}:
                has_assertion = True
        if not isinstance(node, ast.Assert):
            continue
        has_assertion = True
        if _is_tautological_assert(node.test) or _is_static_expression(node.test):
            findings.append(f"{test.reference}: contains a constant or tautological assertion")
    if not has_assertion:
        findings.append(f"{test.reference}: contains no executable assertion")
    return list(dict.fromkeys(findings))


def _text_test_findings(test: AcceptanceTest) -> list[str]:
    body = test.body
    findings: list[str] = []
    current_name = re.escape(test.symbol.rsplit("::", 1)[-1])
    delegated = re.search(r"\btest_[A-Za-z0-9_]+\s*\(", body)
    if delegated and not re.search(rf"\b{current_name}\s*\(", delegated.group(0)):
        findings.append(f"{test.reference}: delegates acceptance to another test")
    placebo_patterns = (
        r"assert!\s*\(\s*true\s*\)",
        r"assert!\s*\([^)]*\|\|\s*true\s*\)",
        r"assert_eq!\s*\(\s*format!\s*\(",
        r"""assert(?:_eq|_ne)?!\s*\(\s*(["'][^"']*["']|\d+)\s*,\s*\1\s*\)""",
        r"\b(?:todo|unimplemented)!\s*\(",
    )
    if any(re.search(pattern, body, re.IGNORECASE | re.DOTALL) for pattern in placebo_patterns):
        findings.append(f"{test.reference}: contains a constant, stub, or placebo assertion")
    if not re.search(r"\b(?:assert|debug_assert)(?:_eq|_ne)?!\s*\(|\bshould_panic\b", body):
        findings.append(f"{test.reference}: contains no executable assertion or panic expectation")
    return findings


def _called_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_static_expression(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_static_expression(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_static_expression(key) and _is_static_expression(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.JoinedStr):
        return all(
            isinstance(item, ast.Constant)
            or isinstance(item, ast.FormattedValue)
            and _is_static_expression(item.value)
            for item in node.values
        )
    if isinstance(node, ast.BinOp):
        return _is_static_expression(node.left) and _is_static_expression(node.right)
    if isinstance(node, ast.UnaryOp):
        return _is_static_expression(node.operand)
    if isinstance(node, ast.Compare):
        return _is_static_expression(node.left) and all(
            _is_static_expression(item) for item in node.comparators
        )
    if isinstance(node, ast.Call) and _called_name(node.func) in {"str", "repr", "format"}:
        return all(_is_static_expression(arg) for arg in node.args)
    return False


def _is_tautological_assert(node: ast.AST) -> bool:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.Or):
            return any(
                isinstance(item, ast.Constant) and item.value is True for item in node.values
            )
        if isinstance(node.op, ast.And):
            return any(
                isinstance(item, ast.Constant) and item.value is False for item in node.values
            )
    return False


def _structured_run_blocks(content: str) -> list[dict[str, str]]:
    if not {"run_url", "commit_sha", "utc_timestamp"} <= {
        match.group("key") for match in _FIELD_RE.finditer(content)
    }:
        return []
    chunks = re.split(r"(?m)^##\s+Run\s*$", content)[1:]
    if not chunks:
        chunks = [content]
    return [
        {
            match.group("key"): _strip_markdown_value(match.group("value"))
            for match in _FIELD_RE.finditer(chunk)
        }
        for chunk in chunks
    ]


def _strip_markdown_value(value: str) -> str:
    return value.strip().strip("`").strip()


def _read_committed_file(path: str, commit_shas: list[str], repo_path: str) -> str:
    for sha in reversed(commit_shas):
        result = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            cwd=repo_path,
            text=True,
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
    candidate = Path(repo_path, path)
    try:
        return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("referenced evidence file is missing or unreadable") from exc


def _commit_time(sha: str, repo_path: str) -> datetime | None:
    try:
        value = _run_command(["git", "show", "-s", "--format=%cI", sha], repo_path).strip()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (RuntimeError, ValueError):
        return None
    return parsed.astimezone(UTC)


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _repository_slug(repo_path: str) -> str | None:
    try:
        remote = _run_command(["git", "remote", "get-url", "origin"], repo_path).strip()
    except RuntimeError:
        return None
    patterns = (
        r"^git@github\.com:(?P<slug>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https?://github\.com/(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote, re.IGNORECASE)
        if match:
            return match.group("slug").removesuffix(".git")
    return None


def _is_repo_actions_url(url: str, repo_slug: str) -> bool:
    parsed = urlparse(url)
    expected = f"/{repo_slug}/actions/runs/"
    suffix = parsed.path[len(expected) :] if parsed.path.startswith(expected) else ""
    return (
        parsed.scheme == "https"
        and parsed.netloc.casefold() == "github.com"
        and parsed.path.casefold().startswith(expected.casefold())
        and bool(re.fullmatch(r"\d+(?:/.*)?", suffix))
    )


def _workflow_exists(sha: str, workflow_name: str, repo_path: str) -> bool:
    try:
        paths = _run_command(
            ["git", "ls-tree", "-r", "--name-only", sha, "--", ".github/workflows"],
            repo_path,
        ).splitlines()
    except RuntimeError:
        return False
    expected = workflow_name.strip().casefold()
    for path in paths:
        if not path.endswith((".yml", ".yaml")):
            continue
        try:
            content = _run_command(["git", "show", f"{sha}:{path}"], repo_path)
        except RuntimeError:
            continue
        match = _WORKFLOW_NAME_RE.search(content)
        if match and match.group("name").strip().casefold() == expected:
            return True
    return False


def _path_error(path: str, repo_path: str) -> str | None:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        return "path traversal is forbidden"
    root = Path(repo_path).resolve()
    candidate = (root / Path(*pure.parts)).resolve(strict=False)
    if not candidate.is_relative_to(root):
        return "path resolves outside the repository"
    return None


def _run_command(args: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{args[0]} command failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(detail)
    return result.stdout


__all__ = [
    "AcceptanceArtifactResult",
    "AcceptanceTest",
    "evaluate_acceptance_artifacts",
    "extract_artifact_references",
    "is_assertion_failure",
    "render_acceptance_test_bodies",
    "resolve_acceptance_tests",
    "validation_run_covers_test",
    "validation_run_names_test",
    "validate_structured_file_evidence",
]
