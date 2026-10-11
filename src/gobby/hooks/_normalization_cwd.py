"""Parent-shell directory state across exclusive and fallthrough branches."""

import re
from dataclasses import dataclass, field

from gobby.hooks._normalization_compound import case_arm_tokens
from gobby.hooks._normalization_env import UNRESOLVED_DIRECTORY
from gobby.hooks._normalization_segments import _ShellSegment
from gobby.hooks._normalization_shell import ShellToken, _apply_cd


@dataclass
class _Branch:
    kind: str
    cwd: str | None
    altered: bool = False
    in_condition: bool = False
    condition_altered: bool = False
    loop: int | None = None


@dataclass
class ShellDirectoryState:
    cwd: str | None
    changed: bool = False
    branches: list[_Branch] = field(default_factory=list)
    unstable_loops: frozenset[int] = frozenset()
    altered_loops: set[int] = field(default_factory=set)
    loops_opened: int = 0
    group_isolation: dict[int, bool] = field(default_factory=dict)
    groups: list[tuple[str | None, bool, list[_Branch]] | None] = field(default_factory=list)
    group_entry: bool = False

    def prepare_groups(self, segments: list[_ShellSegment]) -> None:
        """Identify pipeline members and function bodies before consuming braces."""
        openings: list[tuple[int, int]] = []
        for index, segment in enumerate(segments):
            previous = " ".join(t.value for t in segments[index - 1].tokens) if index else ""
            function_body = bool(re.fullmatch(r"(?:function\s+\w+|\w+\s*\(\s*\))", previous))
            for token in segment.tokens:
                if token.quoted:
                    continue
                if token.value == "{":
                    openings.append((index, id(token)))
                    self.group_isolation[id(token)] = (
                        segment.separator_before == "|" or function_body
                    )
                elif token.value == "}" and openings:
                    _, opener = openings.pop()
                    if index + 1 < len(segments) and segments[index + 1].separator_before == "|":
                        self.group_isolation[opener] = True

    def pipeline_stage(self, before: str | None, after: str | None) -> bool:
        return (before == "|" and not self.group_entry) or after == "|"

    def compound_tokens(
        self, tokens: list[ShellToken], gap: str, case_depth: int, pending: bool
    ) -> tuple[list[ShellToken], int, bool]:
        self.group_entry = False
        self.case_boundary(tokens, gap)
        tokens, case_depth, pending = case_arm_tokens(
            tokens, case_depth=case_depth, new_arm=pending or ";;" in gap or ";&" in gap
        )
        while tokens:
            tokens = self.conditional_tokens(tokens)
            if not tokens or tokens[0].quoted or tokens[0].value not in {"case", "esac"}:
                break
            self.case_boundary(tokens, "")
            tokens, case_depth, pending = case_arm_tokens(
                tokens, case_depth=case_depth, new_arm=False
            )
        return tokens, case_depth, pending

    def case_boundary(self, tokens: list[ShellToken], gap: str) -> None:
        """Snapshot case entry, reset exclusive arms, and join their exit states."""
        if not tokens:
            return
        first = tokens[0]
        if not first.quoted and first.value == "esac":
            self._close("case")
        elif not first.quoted and first.value == "case":
            self.branches.append(_Branch("case", self.cwd))
        elif re.search(r";;(?!&)", gap):
            self._restore("case")

    def conditional_tokens(self, tokens: list[ShellToken]) -> list[ShellToken]:
        """Remove executable if/elif prefixes while restoring branch entry cwd."""
        while tokens and not tokens[0].quoted:
            word = tokens[0].value
            if word == "if":
                self.branches.append(_Branch("if", self.cwd, in_condition=True))
            elif word in {"else", "elif"}:
                self._restore("if")
                if self.branches and self.branches[-1].kind == "if":
                    branch = self.branches[-1]
                    if branch.condition_altered:
                        self.cwd = UNRESOLVED_DIRECTORY
                    branch.in_condition = word == "elif"
            elif word == "fi":
                self._close("if")
            elif word == "then":
                if self.branches and self.branches[-1].kind == "if":
                    branch = self.branches[-1]
                    branch.cwd = self.cwd
                    branch.in_condition = False
            elif word in {"for", "while", "until"}:
                self.branches.append(_Branch("loop", self.cwd, loop=self.loops_opened))
                self.loops_opened += 1
                if word == "for":
                    break  # Keep the header for loop binding attribution.
                self._enter_loop_body()
            elif word == "done":
                self._close("loop")
            elif word == "do":
                self._enter_loop_body()
            elif word == "{":
                isolated = self.group_isolation.get(id(tokens[0]), False)
                self.groups.append((self.cwd, self.changed, self.branches) if isolated else None)
                if isolated:
                    self.branches = []
                self.group_entry = True
            elif word == "}":
                if self.groups and (entry := self.groups.pop()) is not None:
                    self.cwd, self.changed, self.branches = entry
                self.group_entry = False
            else:
                break
            tokens = tokens[1:]
        return tokens

    def _enter_loop_body(self) -> None:
        if self.branches and self.branches[-1].kind == "loop":
            if self.branches[-1].loop in self.unstable_loops:
                self.cwd = UNRESOLVED_DIRECTORY
                self.mark_unproven()

    def change(self, target: str | None) -> None:
        self.cwd = _apply_cd(self.cwd, target) if target is not None else UNRESOLVED_DIRECTORY
        self.mark_changed()

    def invalidate(self) -> None:
        self.cwd = None
        self.mark_changed()

    def mark_unproven(self) -> None:
        """Keep script proof monotonic without assuming directory movement."""
        self.changed = True

    def mark_changed(self) -> None:
        """Record directory movement in every enclosing branch."""
        self.mark_unproven()
        for branch in self.branches:
            branch.altered = True
            branch.condition_altered |= branch.in_condition

    def _restore(self, kind: str) -> None:
        if self.branches and self.branches[-1].kind == kind:
            branch = self.branches[-1]
            self.cwd = branch.cwd

    def _close(self, kind: str) -> None:
        if self.branches and self.branches[-1].kind == kind:
            branch = self.branches.pop()
            if branch.altered and branch.loop is not None:
                self.altered_loops.add(branch.loop)
            self.cwd = UNRESOLVED_DIRECTORY if branch.altered else branch.cwd
