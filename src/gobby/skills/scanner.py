"""Safety scanner wrapper for ClawCare static scanning."""

import logging
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}


@lru_cache(maxsize=1)
def _rule_categories() -> dict[str, str]:
    """Build a stable rule ID -> category map from ClawCare's built-in rulesets."""
    import yaml
    from clawcare.scanner import rules as clawcare_rules

    ruleset_dir = Path(clawcare_rules.__file__).resolve().parent.parent / "rulesets" / "default"
    categories: dict[str, str] = {}

    for rule_file in sorted(ruleset_dir.glob("*.yml")):
        category = rule_file.stem.replace("-", "_")
        raw_rules = yaml.safe_load(rule_file.read_text())
        if not isinstance(raw_rules, list):
            continue

        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            rule_id = rule.get("id")
            if isinstance(rule_id, str):
                categories[rule_id] = category

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
    with tempfile.TemporaryDirectory(prefix=f"skill-{name}-") as temp_dir:
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
