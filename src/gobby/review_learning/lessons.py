"""Lesson normalization, tagging, and markdown rendering.

Reserved canonical ``lesson_type`` values are ``reviewer-miss``,
``fixer-induced-defect``, ``recurring-validation-failure``, ``qa-miss``, and
``validation-miss``. Other values remain valid for existing and future
single-class recorders.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from gobby.review_learning.file_paths import extract_lesson_file_paths, path_tag
from gobby.review_learning.fingerprint import fingerprint_tag, occurrence_tag, short_hash

SourceKind = Literal[
    "review_comment",
    "ci_check",
    "agent_review",
    "qa_rejection",
    "static_analysis",
    "test_failure",
    "plan_review",
    "task_validation",
]
LessonDomain = Literal["code", "plan"]
Decision = Literal["confirmed", "no-fix-policy", "stale", "invalid"]
GuardrailTarget = Literal[
    "helper",
    "test",
    "checklist",
    "rule",
    "workflow",
    "pipeline",
    "validation",
    "tool-config",
]
Risk = Literal["low", "medium", "high"]

VALID_SOURCE_KINDS: set[str] = {
    "review_comment",
    "ci_check",
    "agent_review",
    "qa_rejection",
    "static_analysis",
    "test_failure",
    "plan_review",
    "task_validation",
}
SOURCE_KIND_DOMAIN: dict[str, str] = {
    "review_comment": "code",
    "ci_check": "code",
    "agent_review": "code",
    "qa_rejection": "code",
    "static_analysis": "code",
    "test_failure": "code",
    "plan_review": "plan",
    "task_validation": "code",
}
VALID_DECISIONS: set[str] = {"confirmed", "no-fix-policy", "stale", "invalid"}
VALID_GUARDRAIL_TARGETS: set[str] = {
    "helper",
    "test",
    "checklist",
    "rule",
    "workflow",
    "pipeline",
    "validation",
    "tool-config",
}
VALID_RISKS: set[str] = {"low", "medium", "high"}
CI_SOURCE_KINDS: set[str] = {"ci_check", "static_analysis", "test_failure"}
CODE_DOMAIN_EXCLUDED_TAGS = ("lesson-domain:plan",)

UNSCOPED_SCOPE_TAG = "scope:unscoped"
"""Marks a lesson that resolves no file path, so it applies to every file.

Path tags are an open namespace (`path:<short_hash>`) and `tags_none` matches
whole tags only, so "carries no path tag" is inexpressible as a tag filter.
Stamping the absence makes it a bounded query instead of a Python partition
over an over-fetched page.
"""

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CHECK_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class LessonIdentity:
    """Pattern identity derived from a finding payload."""

    pattern_id: str
    pattern_key: str
    lesson_type: str
    promotable: bool


@dataclass(frozen=True)
class NormalizedLesson:
    """Validated lesson ready for memory storage."""

    source_kind: SourceKind
    lesson_domain: LessonDomain
    source: str
    source_review: str
    decision: Decision
    finding: dict[str, Any]
    evidence: dict[str, Any]
    identity: LessonIdentity
    finding_fingerprint: str
    occurrence_key: str
    occurrence_tag: str
    risk: Risk
    repo: str | None
    language: str | None
    guardrail_target: GuardrailTarget | None
    tags: list[str]
    content: str


def validate_source_kind(value: str) -> SourceKind:
    if value not in VALID_SOURCE_KINDS:
        raise ValueError(f"Invalid source_kind: {value}")
    return cast(SourceKind, value)


def derive_lesson_domain(source_kind: str) -> LessonDomain:
    try:
        lesson_domain = SOURCE_KIND_DOMAIN[source_kind]
    except KeyError as exc:
        raise ValueError(f"Unmapped lesson source kind: {source_kind}") from exc
    if lesson_domain not in {"code", "plan"}:
        raise ValueError(
            f"Invalid lesson domain mapping for source kind {source_kind}: {lesson_domain}"
        )
    return cast(LessonDomain, lesson_domain)


def validate_check_key(value: str) -> str:
    if not isinstance(value, str) or _CHECK_KEY_RE.fullmatch(value) is None:
        raise ValueError(f"Invalid check key: {value!r}")
    return value


def validate_decision(value: str) -> Decision:
    if value not in VALID_DECISIONS:
        raise ValueError(f"Invalid decision: {value}")
    return cast(Decision, value)


def validate_risk(value: str) -> Risk:
    if value not in VALID_RISKS:
        raise ValueError(f"Invalid risk: {value}")
    return cast(Risk, value)


def validate_guardrail_target(value: str | None) -> GuardrailTarget | None:
    if value in (None, ""):
        return None
    if value not in VALID_GUARDRAIL_TARGETS:
        raise ValueError(f"Invalid guardrail_target: {value}")
    return cast(GuardrailTarget, value)


def slugify(value: Any, *, max_length: int = 48, hashed: bool = False) -> str:
    """Build a bounded filter-safe slug."""
    text = "" if value is None else str(value)
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    if not slug:
        slug = "unknown"
    if hashed or len(slug) > max_length:
        prefix_len = max(0, max_length - 13)
        prefix = slug[:prefix_len].strip("-") or "value"
        slug = f"{prefix}-{short_hash(text)}"
    return slug[:max_length].strip("-")


def pattern_key_for(pattern_id: str) -> str:
    return slugify(pattern_id, max_length=64, hashed=len(pattern_id) > 52)


def derive_lesson_identity(finding: dict[str, Any]) -> LessonIdentity:
    lesson_type = slugify(finding.get("lesson_type") or "review-signal", max_length=40)
    explicit = _clean_text(finding.get("pattern_id"))
    if explicit:
        _validate_class_scoped_identity(
            pattern_id=explicit,
            lesson_type=lesson_type,
            finding=finding,
        )
        return LessonIdentity(
            pattern_id=explicit,
            pattern_key=pattern_key_for(explicit),
            lesson_type=lesson_type,
            promotable=True,
        )

    principle = _clean_text(finding.get("principle"))
    if principle:
        derived = f"{lesson_type}:{slugify(principle, max_length=72, hashed=True)}"
        return LessonIdentity(
            pattern_id=derived,
            pattern_key=pattern_key_for(derived),
            lesson_type=lesson_type,
            promotable=True,
        )

    basis_value = finding.get("title") or finding.get("message")
    basis = _clean_text(basis_value) if basis_value else _json_blob(finding)
    fallback = f"non-promotable:{short_hash(basis, 16)}"
    return LessonIdentity(
        pattern_id=fallback,
        pattern_key=pattern_key_for(fallback),
        lesson_type=lesson_type,
        promotable=False,
    )


def _validate_class_scoped_identity(
    *,
    pattern_id: str,
    lesson_type: str,
    finding: dict[str, Any],
) -> None:
    if not pattern_id.startswith(("plan-review:", "epic-qa:")):
        return

    raw_check_key = finding.get("check_key")
    if not isinstance(raw_check_key, str):
        raise ValueError("Class-scoped lesson identity requires an explicit check_key")
    check_key = validate_check_key(raw_check_key)

    if pattern_id.startswith("plan-review:"):
        category = _clean_text(finding.get("category"))
        if not category:
            raise ValueError("plan-review lesson identity requires an explicit category")
        expected = f"plan-review:{lesson_type}:{slugify(category)}:{check_key}"
    else:
        expected = f"epic-qa:{lesson_type}:{check_key}"
    if pattern_id != expected:
        raise ValueError(f"Class-scoped pattern_id must be {expected!r}")


def has_verified_fix(evidence: dict[str, Any]) -> bool:
    """Return whether evidence proves a CI/static/test signal was fixed."""
    keys = (
        "verified_fix",
        "verified_fix_ref",
        "confirmed_fix",
        "fix_ref",
        "commit",
        "commit_sha",
        "changes_id",
    )
    return any(evidence.get(key) for key in keys)


def build_tags(
    *,
    source_kind: SourceKind,
    lesson_domain: LessonDomain,
    source: str,
    decision: Decision,
    identity: LessonIdentity,
    finding: dict[str, Any],
    evidence: dict[str, Any],
    finding_fingerprint: str,
    occurrence_key: str,
    repo: str | None,
    language: str | None,
) -> list[str]:
    tags = [
        "review-lesson",
        decision,
        f"source-kind:{source_kind}",
        f"lesson-domain:{lesson_domain}",
        f"source:{slugify(source)}",
        f"pattern:{identity.pattern_key}",
        fingerprint_tag(finding_fingerprint),
        occurrence_tag(occurrence_key),
        f"lesson-type:{identity.lesson_type}",
    ]
    if "check_key" in finding:
        tags.append(f"check-key:{validate_check_key(finding['check_key'])}")
    category = _clean_text(finding.get("category"))
    if category:
        tags.append(f"category:{slugify(category)}")
    tags.extend(_optional_tags(finding=finding, repo=repo, language=language))
    path_tags = [
        tag
        for path in extract_lesson_file_paths(finding=finding, evidence=evidence)
        if (tag := path_tag(path))
    ]
    tags.extend(path_tags or [UNSCOPED_SCOPE_TAG])
    if not identity.promotable:
        tags.append("non-promotable")
    return _dedupe(tags)


def normalize_lesson(
    *,
    source_kind: str,
    source: str,
    source_review: str,
    decision: str,
    finding: dict[str, Any],
    evidence: dict[str, Any],
    finding_fingerprint: str,
    occurrence_key: str,
    repo: str | None,
    language: str | None,
    risk: str,
    lesson_domain: LessonDomain | None = None,
) -> NormalizedLesson:
    validated_source_kind = validate_source_kind(source_kind)
    derived_lesson_domain = derive_lesson_domain(validated_source_kind)
    if lesson_domain is not None and lesson_domain != derived_lesson_domain:
        raise ValueError(
            f"Lesson domain {lesson_domain!r} does not match source kind {validated_source_kind!r}"
        )
    validated_decision = validate_decision(decision)
    validated_risk = validate_risk(risk)
    guardrail_target = validate_guardrail_target(_clean_text(finding.get("guardrail_target")))
    identity = derive_lesson_identity(finding)
    tags = build_tags(
        source_kind=validated_source_kind,
        lesson_domain=derived_lesson_domain,
        source=source,
        decision=validated_decision,
        identity=identity,
        finding=finding,
        evidence=evidence,
        finding_fingerprint=finding_fingerprint,
        occurrence_key=occurrence_key,
        repo=repo,
        language=language,
    )
    content = render_lesson_content(
        source_kind=validated_source_kind,
        source=source,
        source_review=source_review,
        decision=validated_decision,
        finding=finding,
        evidence=evidence,
        identity=identity,
        finding_fingerprint=finding_fingerprint,
        occurrence_key=occurrence_key,
        repo=repo,
        language=language,
        risk=validated_risk,
        guardrail_target=guardrail_target,
    )
    return NormalizedLesson(
        source_kind=validated_source_kind,
        lesson_domain=derived_lesson_domain,
        source=source,
        source_review=source_review,
        decision=validated_decision,
        finding=dict(finding),
        evidence=dict(evidence),
        identity=identity,
        finding_fingerprint=finding_fingerprint,
        occurrence_key=occurrence_key,
        occurrence_tag=occurrence_tag(occurrence_key),
        risk=validated_risk,
        repo=repo,
        language=language,
        guardrail_target=guardrail_target,
        tags=tags,
        content=content,
    )


def render_lesson_content(
    *,
    source_kind: SourceKind,
    source: str,
    source_review: str,
    decision: Decision,
    finding: dict[str, Any],
    evidence: dict[str, Any],
    identity: LessonIdentity,
    finding_fingerprint: str,
    occurrence_key: str,
    repo: str | None,
    language: str | None,
    risk: Risk,
    guardrail_target: GuardrailTarget | None,
) -> str:
    title = _clean_text(finding.get("title") or finding.get("message") or "Review lesson")
    lines = [
        f"# Review Lesson: {title}",
        "",
        "## Identity",
        f"- pattern_id: {identity.pattern_id}",
        f"- pattern_key: {identity.pattern_key}",
        f"- promotable: {json.dumps(identity.promotable)}",
        f"- finding_fingerprint: {finding_fingerprint}",
        f"- occurrence_key: {occurrence_key}",
        "",
        "## Provenance",
        f"- source_kind: {source_kind}",
        f"- source: {source}",
        f"- source_review: {source_review}",
        f"- decision: {decision}",
        f"- risk: {risk}",
        f"- repo: {repo or ''}",
        f"- language: {language or ''}",
        f"- guardrail_target: {guardrail_target or ''}",
        "",
        "## Lesson",
        f"- principle: {_clean_text(finding.get('principle'))}",
        f"- root_cause: {_clean_text(finding.get('root_cause'))}",
        f"- prevention: {_clean_text(finding.get('prevention'))}",
        "",
        "## Diagnostic",
        f"- title: {_clean_text(finding.get('title'))}",
        f"- message: {_clean_text(finding.get('message'))}",
        f"- rule_id: {_clean_text(finding.get('rule_id'))}",
        f"- rule_url: {_clean_text(finding.get('rule_url'))}",
        f"- severity: {_clean_text(finding.get('severity'))}",
        f"- path: {_clean_text(finding.get('path'))}",
        f"- start_line: {_clean_text(finding.get('start_line'))}",
        f"- end_line: {_clean_text(finding.get('end_line'))}",
        f"- symbol: {_clean_text(finding.get('symbol'))}",
        f"- diagnostic_format: {_clean_text(finding.get('diagnostic_format') or 'raw')}",
        f"- suggestion: {_clean_text(finding.get('suggestion'))}",
        f"- query_hints: {_json_blob(finding.get('query_hints'))}",
        "",
        "## Evidence",
        _json_blob(evidence),
    ]
    return "\n".join(lines).strip()


def _optional_tags(*, finding: dict[str, Any], repo: str | None, language: str | None) -> list[str]:
    tags: list[str] = []
    if language:
        tags.append(f"lang:{slugify(language)}")
    if repo:
        tags.append(f"repo:{slugify(repo, hashed=True)}")
    if finding.get("rule_id"):
        tags.append(f"rule:{slugify(finding['rule_id'])}")
    if finding.get("severity"):
        tags.append(f"severity:{slugify(finding['severity'])}")
    return tags


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _json_blob(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def _dedupe(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result
