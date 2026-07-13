"""Refresh .gobby/project.json verification commands."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Any, Literal

from gobby.ai.text_generation import TextGenerationService
from gobby.config.features import ProjectVerificationSynthesisConfig
from gobby.project_verification.candidates import (
    CommandCandidate,
    generate_candidates,
    select_best_candidates,
    verification_dict_from_candidates,
)
from gobby.project_verification.evidence import EvidenceBundle, collect_evidence
from gobby.project_verification.synthesis import RejectedCommand, synthesize_verification_commands

AIMode = Literal["auto", "on", "off"]
logger = logging.getLogger(__name__)


class ProjectVerificationAIError(RuntimeError):
    """AI synthesis was required but did not produce usable commands."""


class ProjectVerificationReadError(RuntimeError):
    """Existing project metadata could not be read safely for an update."""


@dataclass
class RefreshResult:
    """Project verification refresh outcome."""

    root: Path
    project_json_path: Path
    before: dict[str, Any]
    after: dict[str, Any]
    candidates: list[CommandCandidate]
    selected: dict[str, CommandCandidate]
    changed: bool
    diff: str
    warnings: list[str] = field(default_factory=list)
    written: bool = False
    ai_mode: AIMode = "off"
    ai_used: bool = False
    ai_error: str | None = None
    ai_rejected: list[RejectedCommand] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable result."""
        return {
            "project_path": str(self.root),
            "project_json": str(self.project_json_path),
            "changed": self.changed,
            "written": self.written,
            "warnings": list(self.warnings),
            "ai": {
                "mode": self.ai_mode,
                "used": self.ai_used,
                "error": self.ai_error,
                "rejected": [item.to_dict() for item in self.ai_rejected],
            },
            "before": self.before,
            "after": self.after,
            "diff": self.diff,
            "candidates": [candidate.to_prompt_dict() for candidate in self.candidates],
        }


def refresh_project_verification_deterministic(root: Path, *, fix: bool = False) -> RefreshResult:
    """Refresh using deterministic evidence only."""
    bundle = collect_evidence(root)
    _require_intact_project_json_for_fix(bundle, fix=fix)
    candidates = generate_candidates(bundle)
    selected = select_best_candidates(candidates)
    after = verification_dict_from_candidates(selected)
    result = _build_result(
        root=bundle.root,
        before=bundle.existing_verification,
        after=after,
        candidates=candidates,
        selected=selected,
        ai_mode="off",
        warnings=bundle.warnings,
    )
    if fix and result.changed:
        _write_verification(result.project_json_path, after)
        result.written = True
    return result


async def refresh_project_verification(
    root: Path,
    *,
    fix: bool = False,
    ai_mode: AIMode = "auto",
    synthesis_config: ProjectVerificationSynthesisConfig | None = None,
    text_generation_service: TextGenerationService | None = None,
) -> RefreshResult:
    """Refresh verification commands with optional AI synthesis."""
    bundle = collect_evidence(root)
    _require_intact_project_json_for_fix(bundle, fix=fix)
    candidates = generate_candidates(bundle)
    selected = select_best_candidates(candidates)
    ai_error: str | None = None
    ai_used = False
    rejected: list[RejectedCommand] = []

    if ai_mode != "off":
        if text_generation_service is None:
            ai_error = "No text generation service is available."
            if ai_mode == "on":
                raise ProjectVerificationAIError(ai_error)
        else:
            config = synthesis_config or ProjectVerificationSynthesisConfig()
            try:
                synthesis = await synthesize_verification_commands(
                    text_generation_service,
                    config,
                    bundle,
                    candidates,
                )
            except Exception as exc:
                if isinstance(exc, MemoryError):
                    raise
                ai_error = str(exc)
                if ai_mode == "on":
                    raise ProjectVerificationAIError(
                        f"AI verification synthesis failed: {ai_error}"
                    ) from exc
            else:
                ai_used = True
                rejected = synthesis.rejected
                if not synthesis.has_accepted and ai_mode == "on":
                    raise ProjectVerificationAIError(
                        "AI verification synthesis returned no accepted commands."
                    )
                selected.update(synthesis.accepted)

    after = verification_dict_from_candidates(selected)
    result = _build_result(
        root=bundle.root,
        before=bundle.existing_verification,
        after=after,
        candidates=candidates,
        selected=selected,
        ai_mode=ai_mode,
        ai_used=ai_used,
        ai_error=ai_error,
        ai_rejected=rejected,
        warnings=bundle.warnings,
    )
    if fix and result.changed:
        _write_verification(result.project_json_path, after)
        result.written = True
    return result


def _build_result(
    *,
    root: Path,
    before: dict[str, Any],
    after: dict[str, Any],
    candidates: list[CommandCandidate],
    selected: dict[str, CommandCandidate],
    ai_mode: AIMode,
    warnings: list[str],
    ai_used: bool = False,
    ai_error: str | None = None,
    ai_rejected: list[RejectedCommand] | None = None,
) -> RefreshResult:
    normalized_before = _normalize_verification(before)
    normalized_after = _normalize_verification(after)
    return RefreshResult(
        root=root,
        project_json_path=root / ".gobby" / "project.json",
        before=normalized_before,
        after=normalized_after,
        candidates=candidates,
        selected=selected,
        changed=normalized_before != normalized_after,
        diff=_verification_diff(normalized_before, normalized_after),
        warnings=list(warnings),
        ai_mode=ai_mode,
        ai_used=ai_used,
        ai_error=ai_error,
        ai_rejected=ai_rejected or [],
    )


def _require_intact_project_json_for_fix(bundle: EvidenceBundle, *, fix: bool) -> None:
    if not fix or bundle.existing_project_json_intact:
        return
    detail = bundle.warnings[0] if bundle.warnings else "Existing project metadata is unreadable."
    raise ProjectVerificationReadError(f"Refusing to update {bundle.project_json_path}: {detail}")


def _write_verification(project_json_path: Path, verification: dict[str, Any]) -> None:
    project_json_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if project_json_path.exists():
        data = json.loads(project_json_path.read_text(encoding="utf-8"))
    data["verification"] = verification

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{project_json_path.name}.",
        suffix=".tmp",
        dir=str(project_json_path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
            tmp.write("\n")
        os.replace(tmp_name, project_json_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError as exc:
            logger.debug("Failed to clean up temp file %s: %s", tmp_name, exc)
        raise


def _verification_diff(before: dict[str, Any], after: dict[str, Any]) -> str:
    before_lines = _verification_text(before).splitlines()
    after_lines = _verification_text(after).splitlines()
    return "\n".join(
        unified_diff(
            before_lines,
            after_lines,
            fromfile="current",
            tofile="refreshed",
            lineterm="",
        )
    )


def _verification_text(verification: dict[str, Any]) -> str:
    return json.dumps({"verification": verification}, indent=2, sort_keys=True)


def _normalize_verification(verification: dict[str, Any]) -> dict[str, Any]:
    normalized = {key: value for key, value in verification.items() if value}
    custom = normalized.get("custom")
    if isinstance(custom, dict):
        normalized["custom"] = {str(key): value for key, value in sorted(custom.items()) if value}
    return normalized
