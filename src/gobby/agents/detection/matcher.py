"""Fingerprint-cached matching for agent detection manifests."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from typing import Literal

from gobby.agents.detection.safe_regex import (
    InvalidPatternError,
    RegexOutcome,
    SafeRegex,
    compile_safe_regex,
)
from gobby.agents.detection.schema import (
    DetectionManifest,
    DetectionRule,
    DetectionState,
    MatchClause,
    bottom_non_empty_line_count,
    load_manifest,
)

IssueCode = Literal["invalid_pattern", "pattern_timeout"]


@dataclass(frozen=True, slots=True)
class ManifestIssue:
    """A controlled pattern failure associated with one rule."""

    rule_id: str
    code: IssueCode
    pattern: str


@dataclass(frozen=True, slots=True)
class DetectionMatch:
    """Highest-priority rule matched against a pane snapshot."""

    rule_id: str
    state: DetectionState
    reason: str | None
    priority: int


@dataclass(frozen=True, slots=True)
class MatchEvaluation:
    """Detection result plus compile or runtime pattern issues."""

    match: DetectionMatch | None
    issues: tuple[ManifestIssue, ...]

    @property
    def flagged(self) -> bool:
        return bool(self.issues)


@dataclass(frozen=True, slots=True)
class _CompiledClause:
    contains: tuple[str, ...]
    line_regex: tuple[SafeRegex, ...]

    def evaluate(self, text: str, rule_id: str) -> tuple[bool, ManifestIssue | None]:
        folded_text = text.casefold()
        if any(needle not in folded_text for needle in self.contains):
            return False, None
        for pattern in self.line_regex:
            result = pattern.search(text)
            if result.outcome is RegexOutcome.PATTERN_TIMEOUT:
                return False, ManifestIssue(
                    rule_id=rule_id,
                    code="pattern_timeout",
                    pattern=pattern.source,
                )
            if not result.matched:
                return False, None
        return True, None


@dataclass(frozen=True, slots=True)
class _CompiledRule:
    rule: DetectionRule
    match_clause: _CompiledClause
    exclusions: tuple[_CompiledClause, ...]


@dataclass(frozen=True, slots=True)
class CompiledManifest:
    """A validated manifest with its regex patterns compiled once."""

    manifest: DetectionManifest
    fingerprint: str
    rules: tuple[_CompiledRule, ...]
    issues: tuple[ManifestIssue, ...]

    def match(self, pane_snapshot: str) -> MatchEvaluation:
        issues = list(self.issues)
        for compiled_rule in self.rules:
            rule = compiled_rule.rule
            region_text = _select_region(pane_snapshot, rule.region)
            matched, issue = compiled_rule.match_clause.evaluate(region_text, rule.id)
            if issue is not None:
                issues.append(issue)
                continue
            if not matched:
                continue

            excluded = False
            for clause in compiled_rule.exclusions:
                exclusion_matched, issue = clause.evaluate(region_text, rule.id)
                if issue is not None:
                    issues.append(issue)
                    excluded = True
                    break
                if exclusion_matched:
                    excluded = True
                    break
            if excluded:
                continue

            return MatchEvaluation(
                match=DetectionMatch(
                    rule_id=rule.id,
                    state=rule.state,
                    reason=rule.reason,
                    priority=rule.priority,
                ),
                issues=tuple(issues),
            )
        return MatchEvaluation(match=None, issues=tuple(issues))


def _select_region(pane_snapshot: str, region: str) -> str:
    if region == "whole_recent":
        return pane_snapshot
    if region == "prompt_box":
        return _last_prompt_box(pane_snapshot)

    line_count = bottom_non_empty_line_count(region)
    if line_count is None:
        return ""
    lines = [line for line in pane_snapshot.splitlines() if line.strip()]
    return "\n".join(lines[-line_count:])


def _last_prompt_box(pane_snapshot: str) -> str:
    lines = pane_snapshot.splitlines()
    end_index: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].lstrip().startswith(("╰", "└")):
            end_index = index
            break
    if end_index is None:
        return ""
    for index in range(end_index - 1, -1, -1):
        if lines[index].lstrip().startswith(("╭", "┌")):
            return "\n".join(lines[index : end_index + 1])
    return ""


def _compile_clause(
    clause: MatchClause,
    rule_id: str,
) -> tuple[_CompiledClause | None, ManifestIssue | None]:
    patterns: list[SafeRegex] = []
    for pattern in clause.line_regex:
        try:
            patterns.append(compile_safe_regex(pattern))
        except InvalidPatternError:
            return None, ManifestIssue(
                rule_id=rule_id,
                code="invalid_pattern",
                pattern=pattern,
            )
    return (
        _CompiledClause(
            contains=tuple(needle.casefold() for needle in clause.contains),
            line_regex=tuple(patterns),
        ),
        None,
    )


@lru_cache(maxsize=128)
def _compile_fingerprint(fingerprint: str, content: str) -> CompiledManifest:
    manifest = load_manifest(content)
    compiled_rules: list[_CompiledRule] = []
    issues: list[ManifestIssue] = []

    for rule in sorted(manifest.rules, key=lambda item: item.priority, reverse=True):
        match_clause, issue = _compile_clause(rule, rule.id)
        if issue is not None or match_clause is None:
            if issue is not None:
                issues.append(issue)
            continue

        exclusions: list[_CompiledClause] = []
        invalid = False
        for clause in rule.not_:
            compiled_clause, issue = _compile_clause(clause, rule.id)
            if issue is not None or compiled_clause is None:
                if issue is not None:
                    issues.append(issue)
                invalid = True
                break
            exclusions.append(compiled_clause)
        if invalid:
            continue
        compiled_rules.append(
            _CompiledRule(
                rule=rule,
                match_clause=match_clause,
                exclusions=tuple(exclusions),
            )
        )

    return CompiledManifest(
        manifest=manifest,
        fingerprint=fingerprint,
        rules=tuple(compiled_rules),
        issues=tuple(issues),
    )


def compile_manifest(content: str | bytes) -> CompiledManifest:
    """Compile manifest content once per SHA-256 content fingerprint."""

    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    text = content_bytes.decode("utf-8")
    fingerprint = sha256(content_bytes).hexdigest()
    return _compile_fingerprint(fingerprint, text)
