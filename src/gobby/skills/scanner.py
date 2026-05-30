"""Safety scanner wrapper for ClawCare static scanning."""

import logging
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _safe_temp_component(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    cleaned = cleaned.strip("-_")
    return cleaned[:48] or "skill"


SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}
_DEFAULT_CLAWCARE_RULESET = "default"


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


def scan_skill_content(content: str, name: str = "untitled") -> dict[str, Any]:
    """Scan skill content for safety issues.

    Uses ClawCare's deterministic static rules and preserves Gobby's
    existing scan result schema.

    Args:
        content: Skill markdown content to scan
        name: Skill name for reporting

    Returns:
        Dict with scan results:
        - is_safe: bool
        - max_severity: str
        - scan_duration_seconds: float
        - findings: list of finding dicts
        - findings_count: int

    Raises:
        ImportError: If ClawCare is not installed
    """
    from clawcare.discovery import discover
    from clawcare.integrations.codex import CodexAdapter
    from clawcare.scanner.scanner import scan_root

    start = time.monotonic()

    # ClawCare expects an on-disk root so it can apply the Codex skill adapter.
    with tempfile.TemporaryDirectory(prefix=f"skill-{_safe_temp_component(name)}-") as temp_dir:
        temp_path = Path(temp_dir)
        skill_md = temp_path / "SKILL.md"
        skill_md.write_text(content, encoding="utf-8")

        adapter = CodexAdapter()
        roots = discover(adapter, str(temp_path))
        if not roots:
            raise RuntimeError("ClawCare could not discover a skill root")

        raw_findings = []
        for root in roots:
            raw_findings.extend(scan_root(root, adapter.scan_scope(root)))

        raw_findings.sort(key=lambda finding: finding.sort_key())
        findings: list[dict[str, Any]] = []
        max_severity_num = 0

        for finding in raw_findings:
            sev_str = finding.severity.name.upper()
            findings.append(
                {
                    "severity": sev_str,
                    "title": finding.rule_id,
                    "description": finding.explanation,
                    "category": _category_for_rule(finding.rule_id),
                    "remediation": finding.remediation or "",
                    "location": f"{finding.file_path}:{finding.line}"
                    if finding.line
                    else finding.file_path,
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
