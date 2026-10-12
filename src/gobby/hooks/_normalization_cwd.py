"""Parent-shell directory state across exclusive and fallthrough branches."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from gobby.hooks._normalization_bindings import _raw_shell_word_is_literal
from gobby.hooks._normalization_compound import case_arm_tokens
from gobby.hooks._normalization_env import UNRESOLVED_DIRECTORY, env_directory_operands
from gobby.hooks._normalization_segments import _ShellSegment, _split_shell_segments
from gobby.hooks._normalization_shell import (
    ShellToken,
    _apply_cd,
    _literal_cd_target,
    _quoted_tilde_word,
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
    stack: tuple[str | None, ...] | None = ()


@dataclass
class ShellDirectoryState:
    cwd: str | None
    stack: tuple[str | None, ...] | None = ()
    changed: bool = False
    list_changed: bool = False
    list_conditional: bool = False
    in_conditional: bool = False
    list_frames: list[list[bool]] = field(default_factory=list)
    list_ops: list[bool | None] = field(default_factory=list)
    branches: list[_Branch] = field(default_factory=list)
    unstable_loops: frozenset[int] = frozenset()
    altered_loops: set[int] = field(default_factory=set)
    loops_opened: int = 0
    group_isolation: dict[int, bool] = field(default_factory=dict)
    group_last_pipeline: set[int] = field(default_factory=set)
    pipeline_group_changes: list[bool | None] = field(default_factory=list)
    group_functions: dict[int, str] = field(default_factory=dict)
    function_bodies: dict[int, str] = field(default_factory=dict)
    function_scope_unknown: bool = False
    opaque: bool = False
    function_headers: dict[int, int] = field(default_factory=dict)
    directory_functions: set[str] = field(default_factory=set)
    groups: list[
        tuple[
            str | None, bool, list[_Branch], str | None, bool, bool, tuple[str | None, ...] | None
        ]
    ] = field(default_factory=list)
    group_entry: bool = False
    redirection_cwd: str | None = None
    group_closed: bool = False

    def redirect_cwd(self) -> str | None:
        return self.redirection_cwd if self.group_closed else self.cwd

    def invocation_cwd(
        self, parts: list[str], source_parts: tuple[str, ...], base_cwd: Path | None
    ) -> str | None:
        """Resolve env's invocation directory without moving the parent shell."""
        directories = env_directory_operands(parts)
        cwd = self.cwd
        if directories and cwd is None and not self.changed and base_cwd:
            cwd = str(base_cwd)
        for operand in directories:
            cwd = _apply_cd(cwd, operand)
        raw_directories = env_directory_operands(list(source_parts))
        if len(directories) != len(raw_directories) or any(
            not _raw_shell_word_is_literal(raw) or _quoted_tilde_word(value, raw)
            for value, raw in zip(directories, raw_directories, strict=True)
        ):
            return UNRESOLVED_DIRECTORY
        return cwd

    def prepare_groups(
        self, segments: list[_ShellSegment], spans: dict[int, tuple[int, int]] | None = None
    ) -> None:
        """Identify pipeline members and function bodies before consuming braces."""
        openings: list[tuple[int, int]] = []
        for index, segment in enumerate(segments):
            for position, token in enumerate(segment.tokens):
                if token.quoted:
                    continue
                if token.value == "{":
                    header_tokens = (
                        segment.tokens[:position]
                        if position
                        else segments[index - 1].tokens
                        if index
                        else []
                    )
                    function: str | None = None
                    if spans and (name := self.function_bodies.get(spans[id(token)][0])):
                        function = self.group_functions[id(token)] = name
                    if function is None:
                        for start in range(len(header_tokens)):
                            header = header_tokens[start:]
                            match = re.fullmatch(
                                r"(?:function\s+([^\s()]+)(?:\s*\(\s*\))?|([^\s()]+)\s*\(\s*\))",
                                " ".join(t.value for t in header),
                            )
                            if match and all(not t.quoted for t in header):
                                function = match[1] or match[2]
                                self.group_functions[id(token)] = function
                                self.function_headers[id(header[0])] = len(header)
                                break
                    if function is None and not self._brace_command_position(
                        segment.tokens[:position]
                    ):
                        continue  # A brace argument cannot consume a later group's closer.
                    openings.append((index, id(token)))
                    first_group = not any(
                        prior.value == "{" and not prior.quoted
                        for prior in segment.tokens[:position]
                    )
                    if segment.separator_before == "|" and first_group and function is None:
                        self.group_last_pipeline.add(id(token))
                    self.group_isolation[id(token)] = (
                        segment.separator_before == "|" and first_group
                    ) or function is not None
                elif (
                    token.value == "}"
                    and self._brace_command_position(segment.tokens[:position])
                    and openings
                ):
                    _, opener = openings.pop()
                    if index + 1 < len(segments):
                        after = segments[index + 1].separator_before
                        if after == "|":
                            self.group_isolation[opener] = True
                        if after in {"|", "&"}:
                            self.group_last_pipeline.discard(opener)

    @staticmethod
    def _brace_command_position(prefix: list[ShellToken]) -> bool:
        """Recognize the control prefixes consumed before a command-position brace."""
        index = 0
        while index < len(prefix):
            token = prefix[index]
            if token.quoted:
                return False
            if token.value == "time":
                index += 1
                for option in ("-p", "--"):
                    if (
                        index < len(prefix)
                        and not prefix[index].quoted
                        and prefix[index].value == option
                    ):
                        index += 1
                continue
            if token.value not in {
                "{",
                "}",
                "if",
                "elif",
                "then",
                "else",
                "do",
                "while",
                "until",
                "!",
            }:
                return False
            index += 1
        return True

    def pipeline_stage(self, before: str | None, after: str | None) -> bool:
        return (before == "|" and not self.group_entry) or after == "|"

    def end_list(self, before: str | None) -> None:
        """A cd that ran only after another command leaves cwd unknown once its list ends."""
        if before not in {"&&", "||", "|"} and self.list_conditional:
            self.invalidate()
            self.list_conditional = False

    def sequence_separator(self, before: str | None, *, terminates: bool = False) -> None:
        """Invalidate uncertain cwd only for changes in the current and-or list."""
        if before in {None, ";", "\n"}:
            self.list_changed = False
        elif before in {"||", "&"}:
            if self.list_changed and not (before == "||" and terminates):
                self.invalidate()
            if before == "||" and terminates and not self.in_conditional:
                self.list_conditional = False
            if before == "&":
                self.list_changed = False
        elif before not in {"&&", "|"}:
            self.invalidate()
        for isolated in self.list_ops:
            if isolated is None:
                if self.list_frames:
                    parent, within, frame_isolated, conditional = self.list_frames.pop()
                    self.list_changed = parent or (within and not frame_isolated)
                    self.in_conditional = conditional
            else:
                self.list_frames.append([self.list_changed, False, isolated, self.in_conditional])
                self.list_changed = False
                self.in_conditional |= before in {"&&", "||"}
        self.list_ops.clear()

    def compound_tokens(
        self,
        tokens: list[ShellToken],
        gap: str,
        case_depth: int,
        pending: bool,
        command: str,
        spans: dict[int, tuple[int, int]],
        raw: dict[int, str],
    ) -> tuple[list[ShellToken], int, bool]:
        self.group_entry = False
        self.redirection_cwd = self.cwd
        self.group_closed = False
        self.case_boundary(tokens, gap)
        tokens, case_depth, pending = case_arm_tokens(
            tokens,
            case_depth=case_depth,
            new_arm=pending or ";;" in gap or ";&" in gap,
            command=command,
            spans=spans,
            raw=raw,
        )
        while tokens:
            tokens = self.conditional_tokens(tokens)
            if not tokens or tokens[0].quoted or tokens[0].value not in {"case", "esac"}:
                break
            self.case_boundary(tokens, "")
            tokens, case_depth, pending = case_arm_tokens(
                tokens, case_depth=case_depth, new_arm=False, command=command, spans=spans, raw=raw
            )
        return tokens, case_depth, pending

    def case_boundary(self, tokens: list[ShellToken], gap: str) -> None:
        """Snapshot case entry, reset exclusive arms, and join their exit states."""
        if not tokens:
            return
        first = tokens[0]
        if not first.quoted and first.value == "esac":
            self._close("case")
            self.list_ops.append(None)
        elif not first.quoted and first.value == "case":
            self.branches.append(_Branch("case", self.cwd, stack=self.stack))
            self.list_ops.append(False)
        elif re.search(r";;(?!&)", gap):
            self._restore("case")

    def conditional_tokens(self, tokens: list[ShellToken]) -> list[ShellToken]:
        """Remove executable if/elif prefixes while restoring branch entry cwd."""
        while tokens and not tokens[0].quoted:
            if (header := self.function_headers.get(id(tokens[0]))) is not None:
                tokens = tokens[header:]
                continue
            word = tokens[0].value
            if word == "if":
                self.branches.append(_Branch("if", self.cwd, in_condition=True, stack=self.stack))
                self.list_ops.append(False)
            elif word in {"else", "elif"}:
                self._restore("if")
                if self.branches and self.branches[-1].kind == "if":
                    branch = self.branches[-1]
                    if branch.condition_altered:
                        self.cwd = UNRESOLVED_DIRECTORY
                        self.stack = None
                    branch.in_condition = word == "elif"
            elif word == "fi":
                self._close("if")
                self.list_ops.append(None)
            elif word == "then":
                if self.branches and self.branches[-1].kind == "if":
                    branch = self.branches[-1]
                    branch.cwd = self.cwd
                    branch.stack = self.stack
                    branch.in_condition = False
            elif word in {"for", "while", "until"}:
                self.branches.append(
                    _Branch("loop", self.cwd, loop=self.loops_opened, stack=self.stack)
                )
                self.list_ops.append(False)
                self.loops_opened += 1
                if word == "for":
                    break  # Keep the header for loop binding attribution.
                self._enter_loop_body()
            elif word == "done":
                self._close("loop")
                self.list_ops.append(None)
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
                self.pipeline_group_changes.append(
                    False if id(tokens[0]) in self.group_last_pipeline else None
                )
                self.groups.append(
                    (
                        self.cwd,
                        self.changed,
                        self.branches,
                        function,
                        isolated,
                        self.list_conditional,
                        self.stack,
                    )
                )
                if isolated:
                    self.branches = []
                self.group_entry = True
                self.list_ops.append(isolated)
                if function is not None and self.function_scope_unknown:
                    self.cwd, self.stack = UNRESOLVED_DIRECTORY, None
                    self.mark_unproven()
            elif word == "}":
                if self.groups:
                    entry = self.groups.pop()
                    pipeline_changed = self.pipeline_group_changes.pop()
                    self.redirection_cwd = entry[0]
                    self.group_closed = True
                    if entry[4]:
                        self.cwd, self.changed, self.branches = entry[:3]
                        self.list_conditional = entry[5]
                        self.stack = entry[6]
                    if pipeline_changed:
                        self.invalidate()
                self.group_entry = False
                self.list_ops.append(None)
            else:
                break
            tokens = tokens[1:]
        return tokens

    def _enter_loop_body(self) -> None:
        if self.branches and self.branches[-1].kind == "loop":
            if self.branches[-1].loop in self.unstable_loops:
                self.cwd = UNRESOLVED_DIRECTORY
                self.stack = None
                self.mark_unproven()

    def change(self, target: str | None) -> None:
        self.cwd = _apply_cd(self.cwd, target) if target is not None else UNRESOLVED_DIRECTORY
        if target is None:
            self.stack = None
        self.mark_changed()

    def directory_command(self, parts: list[str], *, uncertain: bool) -> None:
        """Track literal pushes and pops; unknown stack operations lose proof."""
        name = parts[0].rsplit("/", 1)[-1]
        if uncertain or self.opaque:
            self.invalidate()
        elif name == "cd":
            self.change(_literal_cd_target(parts))
        elif name == "popd" and parts[1:] in ([], ["--"]) and self.stack:
            self.cwd, self.stack = self.stack[-1], self.stack[:-1]
            self.mark_changed()
        elif name == "pushd":
            arguments = parts[1:]
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
            target = _literal_cd_target(["cd", *arguments])
            if len(arguments) == 1 and target is not None and not re.fullmatch(r"[+-]\d+", target):
                if self.stack is not None:
                    self.stack = (*self.stack, self.cwd)
                self.change(target)
            elif not arguments and self.stack:
                previous = self.cwd
                self.cwd = self.stack[-1]
                self.stack = (*self.stack[:-1], previous)
                self.mark_changed()
            else:
                self.invalidate()
        else:
            self.invalidate()

    def invalidate(self) -> None:
        self.cwd = UNRESOLVED_DIRECTORY
        self.stack = None
        self.mark_changed()

    def apply_opaque_change(self, parts: list[str]) -> None:
        """Calls and eval may change cwd without a provable literal exit directory."""
        if self._clears_stack(parts):
            self.stack = None
            self.mark_changed()
        elif self._changes_directory(parts):
            self.change(None)

    @staticmethod
    def _clears_stack(parts: list[str]) -> bool:
        return bool(parts and parts[0] == "dirs") and any(
            word.startswith("-") and not word.startswith("--") and "c" in word[1:]
            for word in parts[1:]
        )

    def _changes_directory(self, parts: list[str], depth: int = 0) -> bool:
        if not parts:
            return False
        if self._clears_stack(parts):
            return True
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
        state.prepare_groups(segments)
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
        self.list_changed = True
        for index in range(len(self.groups) - 1, -1, -1):
            if self.pipeline_group_changes[index] is not None:
                self.pipeline_group_changes[index] = True
            if self.groups[index][4]:
                break
        for frame in reversed(self.list_frames):
            frame[1] = True
            if frame[2]:
                break
        for entry in self.groups:
            if entry[3] is not None:
                self.directory_functions.add(entry[3])
        for branch in self.branches:
            branch.altered = True
            branch.condition_altered |= branch.in_condition

    def _restore(self, kind: str) -> None:
        if self.branches and self.branches[-1].kind == kind:
            branch = self.branches[-1]
            self.cwd = branch.cwd
            self.stack = branch.stack

    def _close(self, kind: str) -> None:
        if self.branches and self.branches[-1].kind == kind:
            branch = self.branches.pop()
            if branch.altered and branch.loop is not None:
                self.altered_loops.add(branch.loop)
            self.cwd = UNRESOLVED_DIRECTORY if branch.altered else branch.cwd
            self.stack = None if branch.altered else branch.stack
