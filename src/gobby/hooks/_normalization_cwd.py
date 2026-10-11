"""Parent-shell directory state across exclusive and fallthrough branches."""

import re
from dataclasses import dataclass, field

from gobby.hooks._normalization_compound import case_arm_tokens
from gobby.hooks._normalization_env import UNRESOLVED_DIRECTORY
from gobby.hooks._normalization_segments import _ShellSegment, _split_shell_segments
from gobby.hooks._normalization_shell import (
    ShellToken,
    _apply_cd,
    _strip_shell_wrappers,
    scan_shell_command,
)
from gobby.hooks.shell_execution import SHELL_WRAPPER_DEPTH


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
    group_functions: dict[int, str] = field(default_factory=dict)
    function_headers: dict[int, int] = field(default_factory=dict)
    directory_functions: set[str] = field(default_factory=set)
    groups: list[tuple[str | None, bool, list[_Branch], str | None] | None] = field(
        default_factory=list
    )
    group_entry: bool = False

    def prepare_groups(self, segments: list[_ShellSegment]) -> None:
        """Identify pipeline members and function bodies before consuming braces."""
        openings: list[tuple[int, int]] = []
        for index, segment in enumerate(segments):
            previous = " ".join(t.value for t in segments[index - 1].tokens) if index else ""
            for position, token in enumerate(segment.tokens):
                if token.quoted:
                    continue
                if token.value == "{":
                    header = " ".join(t.value for t in segment.tokens[:position]) or previous
                    function = re.fullmatch(
                        r"(?:function\s+([^\s()]+)(?:\s*\(\s*\))?|([^\s()]+)\s*\(\s*\))",
                        header,
                    )
                    if function:
                        self.group_functions[id(token)] = function[1] or function[2]
                        if position:
                            self.function_headers[id(segment.tokens[0])] = position
                    openings.append((index, id(token)))
                    self.group_isolation[id(token)] = (
                        segment.separator_before == "|" or function is not None
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
        if tokens and (header := self.function_headers.get(id(tokens[0]))) is not None:
            tokens = tokens[header:]
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
            elif word == "!":
                pass
            elif word == "time":
                tokens = tokens[1:]
                for option in ("-p", "--"):
                    if tokens and not tokens[0].quoted and tokens[0].value == option:
                        tokens = tokens[1:]
                continue
            elif word == "{":
                isolated = self.group_isolation.get(id(tokens[0]), False)
                function = self.group_functions.get(id(tokens[0]))
                self.groups.append(
                    (self.cwd, self.changed, self.branches, function) if isolated else None
                )
                if isolated:
                    self.branches = []
                self.group_entry = True
            elif word == "}":
                if self.groups and (entry := self.groups.pop()) is not None:
                    self.cwd, self.changed, self.branches = entry[:3]
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
        self.cwd = UNRESOLVED_DIRECTORY
        self.mark_changed()

    def apply_opaque_change(self, parts: list[str]) -> None:
        """Calls and eval may change cwd without a provable literal exit directory."""
        if self._changes_directory(parts):
            self.change(None)

    def _changes_directory(self, parts: list[str], depth: int = 0) -> bool:
        if not parts:
            return False
        if parts[0] in self.directory_functions:
            return True
        if parts[0] != "eval":
            return False
        if depth >= SHELL_WRAPPER_DEPTH:
            return True
        try:
            segments = _split_shell_segments(scan_shell_command(" ".join(parts[1:])).tokens)
        except ValueError:
            return True
        state = ShellDirectoryState(None)
        for segment in segments:
            tokens = state.conditional_tokens(segment.tokens)
            command = _strip_shell_wrappers([token.value for token in tokens])
            if command and command[0] in {"cd", "pushd", "popd"}:
                return True
            if self._changes_directory(command, depth + 1):
                return True
        return False

    def mark_unproven(self) -> None:
        """Keep script proof monotonic without assuming directory movement."""
        self.changed = True

    def mark_changed(self) -> None:
        """Record directory movement in every enclosing branch."""
        self.mark_unproven()
        for entry in self.groups:
            if entry is not None and entry[3] is not None:
                self.directory_functions.add(entry[3])
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
