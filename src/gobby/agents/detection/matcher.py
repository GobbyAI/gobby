"""Fingerprint-cached matching for agent detection manifests."""

from __future__ import annotations

import re
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

# A horizontal rule the provider draws above and below its composer. The top rule
# may carry a label -- Claude Code draws the terminal's name into it, as in
# ``──────── epic-22508-feedback-triage ─`` -- so only the leading run of rule
# characters is required and the line merely has to end on one. A labelled rule that
# stopped delimiting the frame cost the whole composer region, and a region nobody
# can find reads as ``unknown``: no draft is seen and no Enter is ever verified.
_COMPOSER_RULE_RE = re.compile(r"^\s*[─━]{8,}(?:[^\n]*[─━])?\s*$")

# The rules a manifest needs before any composer probe of it can mean anything.
_COMPOSER_RULE_IDS = frozenset({"composer_draft", "composer_empty"})


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
            match = self._evaluate_rule(compiled_rule, pane_snapshot, issues)
            if match is not None:
                return MatchEvaluation(match=match, issues=tuple(issues))
        return MatchEvaluation(match=None, issues=tuple(issues))

    def reads_composer(self) -> bool:
        """Whether this manifest can classify the provider's composer at all.

        Grok's manifest carries no composer rules, so every probe of a Grok pane is
        ``unknown``. That is the provider's steady state, not a frame that went
        missing, and a caller must not read a verdict into it.
        """
        return any(compiled_rule.rule.id in _COMPOSER_RULE_IDS for compiled_rule in self.rules)

    def match_rule(self, rule_id: str, pane_snapshot: str) -> MatchEvaluation:
        """Evaluate one named rule through the compiled matcher."""

        issues = list(self.issues)
        for compiled_rule in self.rules:
            if compiled_rule.rule.id != rule_id:
                continue
            match = self._evaluate_rule(compiled_rule, pane_snapshot, issues)
            return MatchEvaluation(match=match, issues=tuple(issues))
        return MatchEvaluation(match=None, issues=tuple(issues))

    @staticmethod
    def _evaluate_rule(
        compiled_rule: _CompiledRule,
        pane_snapshot: str,
        issues: list[ManifestIssue],
    ) -> DetectionMatch | None:
        rule = compiled_rule.rule
        region_text = _select_region(pane_snapshot, rule.region)
        matched, issue = compiled_rule.match_clause.evaluate(region_text, rule.id)
        if issue is not None:
            issues.append(issue)
            return None
        if not matched:
            return None
        for clause in compiled_rule.exclusions:
            exclusion_matched, issue = clause.evaluate(region_text, rule.id)
            if issue is not None:
                issues.append(issue)
                return None
            if exclusion_matched:
                return None
        return DetectionMatch(
            rule_id=rule.id,
            state=rule.state,
            reason=rule.reason,
            priority=rule.priority,
        )


def _select_region(pane_snapshot: str, region: str) -> str:
    if region == "whole_recent":
        return pane_snapshot
    if region == "prompt_box":
        return _last_prompt_box(pane_snapshot)
    if region == "composer":
        return composer_region(pane_snapshot)

    line_count = bottom_non_empty_line_count(region)
    if line_count is None:
        return ""
    lines = [line for line in pane_snapshot.splitlines() if line.strip()]
    return "\n".join(lines[-line_count:])


def composer_region(pane_snapshot: str) -> str:
    """Return the bottom-most composer frame: rule-delimited, else the last prompt box.

    Claude Code draws the composer between two horizontal rules; Droid draws it
    as a box. Status bars sit below the frame, so they never enter the region.
    An empty string means no complete frame is visible.
    """
    lines = pane_snapshot.splitlines()
    rule_indexes = [index for index, line in enumerate(lines) if _COMPOSER_RULE_RE.match(line)]
    if len(rule_indexes) >= 2:
        return "\n".join(lines[rule_indexes[-2] + 1 : rule_indexes[-1]])
    return _last_prompt_box(pane_snapshot)


def _last_prompt_box(pane_snapshot: str) -> str:
    lines = pane_snapshot.splitlines()
    start_index: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].lstrip().startswith(("╭", "┌")):
            start_index = index
            break

    end_index: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].lstrip().startswith(("╰", "└")):
            end_index = index
            break
    if end_index is None or (start_index is not None and start_index > end_index):
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

    if isinstance(content, str):
        text = content
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content
        text = content.decode("utf-8")
    fingerprint = sha256(content_bytes).hexdigest()
    return _compile_fingerprint(fingerprint, text)
