"""Safety scanner wrapper for ClawCare static scanning."""

import hashlib
import logging
import tempfile
import time
from collections import OrderedDict
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.skills.parser import ParsedSkill

logger = logging.getLogger(__name__)


def safe_temp_component(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    cleaned = cleaned.strip("-_")
    return cleaned[:48] or "skill"


SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}
_DEFAULT_CLAWCARE_RULESET = "default"

# Source types whose content arrives from outside the local machine. These
# fail closed when the scanner is unavailable and are rescanned at serve time.
EXTERNAL_SOURCE_TYPES: frozenset[str] = frozenset({"hub", "github", "zip", "url"})


def is_external_source(source_type: str | None) -> bool:
    """Whether a skill source type is external (untrusted) tier."""
    return source_type in EXTERNAL_SOURCE_TYPES


@lru_cache(maxsize=1)
def _rule_categories() -> dict[str, str]:
    """Build a stable rule ID -> category map from ClawCare's built-in rulesets.

    ClawCare exposes public helpers to enumerate and load builtin rulesets, but
    not per-file category metadata. We validate the builtin ruleset through the
    public API first, then read the validated ruleset directory to retain the
    existing category mapping by filename.

    If this loader ever needs async I/O, replace this sync ``@lru_cache`` path
    with an async-safe cache and async entrypoint instead of mixing file reads
    into the event loop.
    """
    import yaml
    from clawcare.scanner import rules as clawcare_rules

    builtin_rulesets = set(clawcare_rules.list_builtin_rulesets())
    ruleset_dir = clawcare_rules._RULESETS_DIR / _DEFAULT_CLAWCARE_RULESET
    clawcare_source = clawcare_rules.__file__

    if _DEFAULT_CLAWCARE_RULESET not in builtin_rulesets:
        raise RuntimeError(
            "ClawCare builtin ruleset is unavailable: "
            f"ruleset={_DEFAULT_CLAWCARE_RULESET!r}, "
            f"available={sorted(builtin_rulesets)!r}, "
            f"ruleset_dir={ruleset_dir}, clawcare_rules={clawcare_source}"
        )
    if not ruleset_dir.is_dir():
        raise RuntimeError(
            "ClawCare builtin ruleset directory is missing: "
            f"ruleset_dir={ruleset_dir}, clawcare_rules={clawcare_source}"
        )
    if not clawcare_rules.load_builtin_ruleset(_DEFAULT_CLAWCARE_RULESET):
        raise RuntimeError(
            "ClawCare builtin ruleset loaded no rules: "
            f"ruleset_dir={ruleset_dir}, clawcare_rules={clawcare_source}"
        )

    rule_files = sorted((*ruleset_dir.glob("*.yml"), *ruleset_dir.glob("*.yaml")))
    if not rule_files:
        raise RuntimeError(
            "ClawCare builtin ruleset directory contains no YAML files: "
            f"ruleset_dir={ruleset_dir}, clawcare_rules={clawcare_source}"
        )

    categories: dict[str, str] = {}

    for rule_file in rule_files:
        category = rule_file.stem.replace("-", "_")
        raw_rules = yaml.safe_load(rule_file.read_text(encoding="utf-8"))
        if not isinstance(raw_rules, list):
            continue

        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            rule_id = rule.get("id")
            if isinstance(rule_id, str):
                categories[rule_id] = category

    if not categories:
        raise RuntimeError(
            "ClawCare builtin ruleset did not yield any categorized rules: "
            f"ruleset_dir={ruleset_dir}, clawcare_rules={clawcare_source}"
        )

    return categories


def _category_for_rule(rule_id: str) -> str:
    if rule_id.startswith("MANIFEST_"):
        return "manifest_policy"
    return _rule_categories().get(rule_id, "security")


def _materialize_scan_file(root: Path, rel_path: str, file_content: str) -> int:
    """Write one skill file into the scan root, refusing path escapes.

    Returns the encoded size in bytes so the caller can size the scan cap.
    """
    if rel_path == "SKILL.md":
        raise ValueError("Skill file path 'SKILL.md' would shadow the skill body")
    target = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if not target.is_relative_to(root_resolved) or target == root_resolved:
        raise ValueError(f"Unsafe skill file path escapes the scan root: {rel_path!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = file_content.encode("utf-8")
    target.write_bytes(data)
    return len(data)


def scan_skill_content(
    content: str,
    name: str = "untitled",
    files: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Scan skill content for safety issues.

    Uses ClawCare's deterministic static rules and preserves Gobby's
    existing scan result schema.

    Args:
        content: Skill markdown content to scan
        name: Skill name for reporting
        files: Optional auxiliary skill files (relative path -> text content)
            scanned alongside SKILL.md — references/, scripts/, assets, etc.

    Returns:
        Dict with scan results:
        - is_safe: bool
        - max_severity: str
        - scan_duration_seconds: float
        - findings: list of finding dicts
        - findings_count: int

    Raises:
        ImportError: If ClawCare is not installed
        ValueError: If an auxiliary file path escapes the scan root
    """
    from clawcare.discovery import discover
    from clawcare.integrations.codex import CodexAdapter
    from clawcare.scanner.scanner import scan_root

    start = time.monotonic()

    # ClawCare expects an on-disk root so it can apply the Codex skill adapter.
    with tempfile.TemporaryDirectory(prefix=f"skill-{safe_temp_component(name)}-") as temp_dir:
        temp_path = Path(temp_dir)
        skill_md = temp_path / "SKILL.md"
        skill_md.write_text(content, encoding="utf-8")

        max_file_bytes = len(content.encode("utf-8"))
        for rel_path, file_content in (files or {}).items():
            size = _materialize_scan_file(temp_path, rel_path, file_content)
            max_file_bytes = max(max_file_bytes, size)

        adapter = CodexAdapter()
        roots = discover(adapter, str(temp_path))
        if not roots:
            raise RuntimeError("ClawCare could not discover a skill root")

        raw_findings = []
        for root in roots:
            scope = adapter.scan_scope(root)
            # The loader persists any non-binary file, so the adapter's default
            # extension globs are too narrow, and the default 512KB size cap
            # would silently skip oversized files instead of scanning them.
            scope["include_globs"] = ["*"]
            scope["max_file_size_kb"] = max(512, max_file_bytes // 1024 + 1)
            raw_findings.extend(scan_root(root, scope))

        raw_findings.sort(key=lambda finding: finding.sort_key())
        findings: list[dict[str, Any]] = []
        max_severity_num = 0

        for finding in raw_findings:
            sev_str = finding.severity.name.upper()
            try:
                file_ref = str(Path(finding.file_path).resolve().relative_to(temp_path.resolve()))
            except (OSError, ValueError):
                file_ref = finding.file_path
            findings.append(
                {
                    "severity": sev_str,
                    "title": finding.rule_id,
                    "description": finding.explanation,
                    "category": _category_for_rule(finding.rule_id),
                    "remediation": finding.remediation or "",
                    "location": f"{file_ref}:{finding.line}" if finding.line else file_ref,
                }
            )
            max_severity_num = max(max_severity_num, SEVERITY_ORDER.get(sev_str, 0))

        severity_names = {v: k for k, v in SEVERITY_ORDER.items()}
        max_severity = severity_names.get(max_severity_num, "INFO")
        duration = time.monotonic() - start

        return {
            "is_safe": max_severity_num < SEVERITY_ORDER["HIGH"],
            "max_severity": max_severity,
            "scan_duration_seconds": round(duration, 3),
            "findings": findings,
            "findings_count": len(findings),
        }


def scan_parsed_skill(parsed_skill: "ParsedSkill", name: str | None = None) -> dict[str, Any]:
    """Scan a parsed skill's full text surface: SKILL.md plus all loaded files.

    The loader only loads non-binary files, so every loaded file is text and
    gets materialized into the scan root — a payload hidden in references/,
    scripts/, or an asset is scanned exactly like the skill body.
    """
    files = {lf.path: lf.content for lf in parsed_skill.loaded_files or []}
    return scan_skill_content(
        parsed_skill.content,
        name=name or parsed_skill.name or "untitled",
        files=files,
    )


# Serve-time scan results keyed by (extension, content sha256). Each unique
# content is scanned once per process; the extension is part of the key because
# it selects ClawCare's scan mode (markdown AST vs plain regex).
_SERVE_SCAN_CACHE_MAX_SIZE = 256
_serve_scan_cache: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()


def reset_serve_scan_cache() -> None:
    """Clear the serve-time scan cache (test isolation)."""
    _serve_scan_cache.clear()


def scan_served_content(content: str, name: str, path: str = "SKILL.md") -> dict[str, Any]:
    """Scan content as it is served into agent context, cached by content hash.

    External-tier skills are rescanned at serve time so content that predates
    a scan (or slipped past install-time scanning) is audited before it
    materializes into an agent's context.

    Args:
        content: The exact text about to be served
        name: Skill name for reporting
        path: Relative path of the served file; "SKILL.md" for the skill body

    Raises:
        ImportError: If ClawCare is not installed
    """
    key = (
        Path(path).suffix.lower(),
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
    cached = _serve_scan_cache.get(key)
    if cached is not None:
        _serve_scan_cache.move_to_end(key)
        return cached
    if path == "SKILL.md":
        result = scan_skill_content(content, name=name)
    else:
        result = scan_skill_content("", name=name, files={path: content})
    _serve_scan_cache[key] = result
    _serve_scan_cache.move_to_end(key)
    while len(_serve_scan_cache) > _SERVE_SCAN_CACHE_MAX_SIZE:
        _serve_scan_cache.popitem(last=False)
    return result
