"""Deterministic acceptance-artifact and provenance checks."""

from __future__ import annotations

import ast
import asyncio
import re
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from gobby.utils.daemon_git import GitFailed, GitOk, GitTimeout, daemon_git

_ARTIFACT_REF_RE = re.compile(
    r"^\s*(?:>\s*)?(?:[-*+]\s+|\d+[.)]\s+)?"
    r"(?:\*\*)?(?P<kind>test|file)(?:\*\*)?:"
    r"(?:\s*`(?P<quoted>[^`]+)`|\s+(?P<bare>[^\s,;]+))",
    re.IGNORECASE | re.MULTILINE,
)
# An unbackticked token only counts as a reference when it is shaped like one.
# Backticks are an unconditional statement of intent, so they skip this filter and a
# malformed backticked reference still fails loudly.
_BARE_REF_SHAPE_RE = re.compile(
    r"::|/|\.(?:py|rs|ts|tsx|js|mjs|cjs|sh|md|ya?ml|json|toml|sql)$",
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
    """One exact test reference and its commit-pinned body."""

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
        if match.group("quoted") is None:
            value = value.rstrip("`")
            if not _BARE_REF_SHAPE_RE.search(value):
                continue
        if value and value not in references:
            references.append(value)
    return tuple(references)


def malformed_test_reference_findings(criteria: str) -> tuple[str, ...]:
    """Return diagnostics for named tests that omit a path or symbol."""
    return tuple(
        f"{reference}: malformed test reference; expected path::test_symbol"
        for reference in extract_artifact_references(criteria, "test")
        if parse_test_reference(reference) is None
    )


async def evaluate_acceptance_artifacts_async(
    *,
    criteria: str,
    repo_path: str,
    commit_shas: list[str],
) -> AcceptanceArtifactResult:
    """Resolve named tests, reject placebo bodies, and verify local evidence provenance."""
    tests, resolution_findings = await resolve_acceptance_tests_async(
        criteria, repo_path, commit_shas
    )
    findings = list(resolution_findings)
    for test in tests:
        findings.extend(_test_body_findings(test))

    evidence_files = extract_artifact_references(criteria, "file")
    findings.extend(
        await validate_structured_file_evidence_async(
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


def evaluate_acceptance_artifacts(
    *, criteria: str, repo_path: str, commit_shas: list[str]
) -> AcceptanceArtifactResult:
    """Offline synchronous facade for direct-library consumers."""
    return asyncio.run(
        evaluate_acceptance_artifacts_async(
            criteria=criteria,
            repo_path=repo_path,
            commit_shas=commit_shas,
        )
    )


async def resolve_acceptance_tests_async(
    criteria: str,
    repo_path: str,
    commit_shas: list[str],
) -> tuple[tuple[AcceptanceTest, ...], tuple[str, ...]]:
    """Resolve every named acceptance test from the last linked commit."""
    tests: list[AcceptanceTest] = []
    findings = list(malformed_test_reference_findings(criteria))
    for reference in extract_artifact_references(criteria, "test"):
        parsed = parse_test_reference(reference)
        if parsed is None:
            continue
        path, symbol = parsed
        path_error = _path_error(path, repo_path)
        if path_error:
            findings.append(f"{reference}: {path_error}")
            continue
        if not commit_shas:
            findings.append(f"{reference}: a linked commit is required to resolve the test body")
            continue
        try:
            body = await _resolve_test_body(path, symbol, repo_path, commit_shas[-1])
        except (OSError, RuntimeError, ValueError) as exc:
            findings.append(f"{reference}: could not resolve the committed test body: {exc}")
            continue
        tests.append(AcceptanceTest(reference, path, symbol, body))
    return tuple(tests), tuple(findings)


def resolve_acceptance_tests(
    criteria: str, repo_path: str, commit_shas: list[str]
) -> tuple[tuple[AcceptanceTest, ...], tuple[str, ...]]:
    """Offline synchronous facade for direct-library consumers."""
    return asyncio.run(resolve_acceptance_tests_async(criteria, repo_path, commit_shas))


def render_acceptance_test_bodies(tests: tuple[AcceptanceTest, ...]) -> str:
    """Render exact named test bodies for criteria-review evidence."""
    if not tests:
        return "Named acceptance tests: none."
    parts = ["Named acceptance tests (exact bodies from the last linked commit):"]
    for test in tests:
        parts.append(f"\n### {test.reference}\n{test.body}")
    return "\n".join(parts)


def is_assertion_failure(output: str | None) -> bool:
    """Return whether command output proves a test assertion or panic failure."""
    if not output or _NON_ASSERTION_FAILURE_RE.search(output):
        return False
    return _ASSERTION_FAILURE_RE.search(output) is not None


def validation_run_names_test(
    core_command: str | None,
    output: str | None,
    test: AcceptanceTest,
) -> bool:
    """Return whether a credited core command identifies the exact test."""
    if core_command is None:
        return False
    evidence = f"{core_command}\n{output or ''}"
    if Path(test.path).suffix == ".rs" and rust_validation_run_names_test(evidence, test):
        return True
    symbol_variants = (test.symbol, test.symbol.replace(".", "::"))
    return any(symbol in evidence for symbol in symbol_variants) and (
        test.path in evidence or Path(test.path).name in evidence
    )


def validation_run_covers_test(
    core_command: str | None,
    output: str | None,
    test: AcceptanceTest,
) -> bool:
    """Return whether a credited core command covers the named test or its file."""
    if core_command is None:
        return False
    evidence = f"{core_command}\n{output or ''}"
    if test.path in evidence or test.reference in evidence:
        return True
    if Path(test.path).suffix == ".rs":
        return rust_validation_run_names_test(evidence, test)
    names = (
        Path(test.path).name,
        test.symbol,
        test.symbol.replace(".", "::"),
        test.symbol.replace("::", "."),
    )
    return any(_line_contains_word(evidence, name) for name in names)


def rust_validation_run_names_test(evidence: str, test: AcceptanceTest) -> bool:
    """Return whether one evidence line names a Rust test under its source module."""
    if Path(test.path).suffix != ".rs":
        return False

    path_parts = list(PurePosixPath(test.path).with_suffix("").parts)
    if len(path_parts) >= 3 and path_parts[0] == "crates":
        path_parts = path_parts[2:]

    is_integration_test = bool(path_parts and path_parts[0] == "tests")
    if path_parts and path_parts[0] in {"src", "tests"}:
        path_parts = path_parts[1:]
    if path_parts and path_parts[-1] in {"lib", "main", "mod"}:
        path_parts.pop()
    if not path_parts:
        return False

    module_prefix = "::".join(re.escape(part) for part in path_parts)
    symbol = re.escape(test.symbol)
    module_test = re.compile(rf"\b{module_prefix}(?:::[A-Za-z0-9_]+)*::{symbol}\b")
    integration_stem = re.compile(rf"\b{re.escape(PurePosixPath(test.path).stem)}\b")
    symbol_word = re.compile(rf"\b{symbol}\b")

    return any(
        module_test.search(line)
        or (is_integration_test and integration_stem.search(line) and symbol_word.search(line))
        for line in evidence.splitlines()
    )


def _line_contains_word(value: str, word: str) -> bool:
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])")
    return any(pattern.search(line) for line in value.splitlines())


async def validate_structured_file_evidence_async(
    *,
    evidence_files: tuple[str, ...],
    repo_path: str,
    commit_shas: list[str],
) -> tuple[str, ...]:
    """Validate structured CI evidence using only repository-local facts."""
    findings: list[str] = []
    repo_slug = await _repository_slug(repo_path)
    for path in evidence_files:
        path_error = _path_error(path, repo_path)
        if path_error:
            findings.append(f"{path}: {path_error}")
            continue
        try:
            content = await _read_committed_file(path, commit_shas, repo_path)
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
            commit_time = await _commit_time(sha, repo_path)
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
            if commit_time is not None and not await _workflow_exists(
                sha, fields["workflow_name"], repo_path
            ):
                findings.append(
                    f"{label}: producer workflow {fields['workflow_name']!r} "
                    f"is absent from cited commit {sha}"
                )
    return tuple(findings)


def validate_structured_file_evidence(
    *, evidence_files: tuple[str, ...], repo_path: str, commit_shas: list[str]
) -> tuple[str, ...]:
    """Offline synchronous facade for direct-library consumers."""
    return asyncio.run(
        validate_structured_file_evidence_async(
            evidence_files=evidence_files,
            repo_path=repo_path,
            commit_shas=commit_shas,
        )
    )


def parse_test_reference(reference: str) -> tuple[str, str] | None:
    if "::" not in reference:
        return None
    path, symbol = reference.split("::", 1)
    path = path.strip()
    symbol = symbol.strip()
    return (path, symbol) if path and symbol else None


async def _resolve_test_body(path: str, symbol: str, repo_path: str, commit_sha: str) -> str:
    source = await _read_test_file_from_commit(path, commit_sha, repo_path)
    if Path(path).suffix.casefold() == ".py":
        return _extract_python_test_body(source, symbol)
    return _extract_braced_test_body(source, symbol)


async def _read_test_file_from_commit(path: str, commit_sha: str, repo_path: str) -> str:
    result = await daemon_git.run(("show", f"{commit_sha}:{path}"), cwd=repo_path, timeout=30)
    if not isinstance(result, GitOk):
        raise RuntimeError(f"last linked commit {commit_sha[:12]} does not contain {path}")
    return result.stdout


def _extract_python_test_body(source: str, symbol: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeError("committed test file is not parseable Python") from exc
    requested = tuple(part for part in re.split(r"::|\.", symbol) if part)
    matches: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit(nodes: list[ast.stmt], parents: tuple[str, ...]) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                visit(node.body, (*parents, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = (*parents, node.name)
                if requested and qualified[-len(requested) :] == requested:
                    matches.append(node)
                visit(node.body, qualified)

    visit(tree.body, ())
    if len(matches) != 1:
        raise RuntimeError(f"expected one matching symbol, found {len(matches)}")
    node = matches[0]
    start_line = min(
        (decorator.lineno for decorator in node.decorator_list),
        default=node.lineno,
    )
    if node.end_lineno is None:
        raise RuntimeError("committed Python symbol has no source boundary")
    return "".join(source.splitlines(keepends=True)[start_line - 1 : node.end_lineno])


def _extract_braced_test_body(source: str, symbol: str) -> str:
    name = symbol.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    escaped = re.escape(name)
    patterns = (
        rf'(?m)^[ \t]*(?:(?:pub(?:\([^)]*\))?|async|unsafe|const|extern\s+"[^"]+")\s+)*fn\s+{escaped}\b',
        rf"(?m)^[ \t]*(?:export\s+)?(?:async\s+)?function\s+{escaped}\b",
        rf"(?m)^[ \t]*func(?:\s+\([^)]*\))?\s+{escaped}\b",
        rf"(?m)^[ \t]*(?:const|let|var)\s+{escaped}\s*=.*?=>",
    )
    matches = [match for pattern in patterns for match in re.finditer(pattern, source)]
    if len(matches) != 1:
        raise RuntimeError(f"expected one matching symbol, found {len(matches)}")
    declaration_start = matches[0].start()
    body_start = source.find("{", matches[0].end())
    if body_start < 0:
        raise RuntimeError("matching symbol has no braced body")
    body_end = _matching_brace_end(source, body_start)
    start = _include_symbol_attributes(source, declaration_start)
    return source[start:body_end]


def _include_symbol_attributes(source: str, declaration_start: int) -> int:
    start = declaration_start
    while start > 0:
        previous_end = start - 1
        previous_start = source.rfind("\n", 0, previous_end) + 1
        previous = source[previous_start:previous_end].strip()
        if not previous.startswith(("#[", "///", "@")):
            break
        start = previous_start
    return start


def _matching_brace_end(source: str, opening: int) -> int:
    depth = 0
    index = opening
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            line_comment = char != "\n"
        elif block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 1
        elif quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char == "/" and following == "/":
            line_comment = True
            index += 1
        elif char == "/" and following == "*":
            block_comment = True
            index += 1
        elif char in {'"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise RuntimeError("matching symbol has an unclosed braced body")


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
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = _called_name(node.func)
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


async def _read_committed_file(path: str, commit_shas: list[str], repo_path: str) -> str:
    for sha in reversed(commit_shas):
        result = await daemon_git.run(("show", f"{sha}:{path}"), cwd=repo_path, timeout=30)
        if isinstance(result, GitOk):
            return result.stdout
        if isinstance(result, GitTimeout) or (
            isinstance(result, GitFailed) and result.returncode is None
        ):
            raise RuntimeError("git is unavailable while resolving evidence")
    candidate = Path(repo_path, path)
    try:
        return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("referenced evidence file is missing or unreadable") from exc


async def _commit_time(sha: str, repo_path: str) -> datetime | None:
    try:
        value = (await _run_command(["show", "-s", "--format=%cI", sha], repo_path)).strip()
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


async def _repository_slug(repo_path: str) -> str | None:
    try:
        remote = (await _run_command(["remote", "get-url", "origin"], repo_path)).strip()
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


async def _workflow_exists(sha: str, workflow_name: str, repo_path: str) -> bool:
    try:
        paths = (
            await _run_command(
                ["ls-tree", "-r", "--name-only", sha, "--", ".github/workflows"],
                repo_path,
            )
        ).splitlines()
    except RuntimeError:
        return False
    expected = workflow_name.strip().casefold()
    for path in paths:
        if not path.endswith((".yml", ".yaml")):
            continue
        try:
            content = await _run_command(["show", f"{sha}:{path}"], repo_path)
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


async def _run_command(args: list[str], cwd: str) -> str:
    result = await daemon_git.run(args, cwd=cwd, timeout=30)
    if not isinstance(result, GitOk):
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
    "rust_validation_run_names_test",
    "validation_run_covers_test",
    "validation_run_names_test",
    "validate_structured_file_evidence",
]
