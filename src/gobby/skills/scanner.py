"""Safety scanner wrapper for Cisco skill-scanner.

Thin wrapper around the skill-scanner package that runs deterministic
static-rule scanning and risk scoring. No API key required.
"""

import logging
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}


def scan_skill_content(content: str, name: str = "untitled") -> dict[str, Any]:
    """Scan skill content for safety issues.

    Uses Cisco's skill-scanner deterministic static rules.
    No API key required.

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
        ImportError: If skill-scanner is not installed
    """
    from skill_scanner.core.scanner import SkillScanner

    start = time.monotonic()

    # Create a temporary directory because SkillScanner v2 expects a directory
    with tempfile.TemporaryDirectory(prefix=f"skill-{name}-") as temp_dir:
        temp_path = Path(temp_dir)
        skill_md = temp_path / "SKILL.md"
        with open(skill_md, "w", encoding="utf-8") as f:
            f.write(content)

        scanner = SkillScanner()
        # lenient=True ensures it parses SKILL.md even if fields are malformed/missing
        result = scanner.scan_skill(temp_path, lenient=True)

        findings: list[dict[str, Any]] = []
        max_severity_num = 0

        for finding in result.findings:
            sev_str = finding.severity.value.upper()
            findings.append(
                {
                    "severity": sev_str,
                    "title": finding.title,
                    "description": finding.description,
                    "category": finding.category.value,
                    "remediation": finding.remediation or "",
                    "location": f"{finding.file_path}:{finding.line_number}"
                    if finding.line_number
                    else "",
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
