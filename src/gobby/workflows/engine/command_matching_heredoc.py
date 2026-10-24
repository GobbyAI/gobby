"""Heredoc text removal and execution classification for one shell segment."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from gobby.hooks._normalization_shell import HeredocBody, ShellToken, _strip_shell_wrappers
from gobby.hooks._shell_execution_words import _unwrap
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.provider_launch_guard import _SHELLS, _shell_stdin

# Every other stdin consumer conservatively retains executable heredoc text.
_DATA_CONSUMERS = frozenset({"cat", "tee", "git", "gh"})
_OUTPUT_PROCESS_SUBSTITUTION = re.compile(r">(?:\\\n)*\(")


def heredoc_free_text(command: str, cursor: int, end: int, heredocs: Sequence[HeredocBody]) -> str:
    """Remove only bodies starting within this source span."""
    parts: list[str] = []
    for heredoc in heredocs:
        if cursor <= heredoc.start < end:
            parts.append(command[cursor : heredoc.start])
            cursor = min(heredoc.end, end)
    parts.append(command[cursor:end])
    return "".join(parts)


@dataclass(frozen=True)
class HeredocExecution:
    stage_of: list[int]
    consumer_executes: list[bool]
    downstream_shell: list[bool]
    raw_outproc: bool
    source: Literal["shell", "language"]
    language_stage: list[bool]

    def may_execute(self, heredoc: HeredocBody, opener: int) -> bool:
        if not heredoc.terminated or self.raw_outproc:
            return True
        owner = self.stage_of[opener]
        return self.consumer_executes[owner] or self.downstream_shell[owner]

    def runs_language(self, heredoc: HeredocBody, opener: int) -> bool:
        """Whether known language source has no potential shell consumer."""
        owner = self.stage_of[opener]
        return (
            heredoc.terminated
            and self.language_stage[owner]
            and not self.downstream_shell[owner]
            and not self.raw_outproc
        )


def classify_heredoc_execution(
    tokens: list[ShellToken], raw: str, stages: list[list[str]], language_stage: list[bool]
) -> HeredocExecution:
    """Classify every stage once, for constant-time checks of its heredocs."""
    stage_of: list[int] = []
    stage = 0
    for token in tokens:
        stage_of.append(stage)
        if not token.quoted and token.value == "|":
            stage += 1
    consumers = [_strip_shell_wrappers(words) for words in stages]
    consumer_executes = [
        bool(words and shell_command_name(words[0]) not in _DATA_CONSUMERS) for words in consumers
    ]
    unwrapped = [_unwrap(words) for words in stages]
    downstream_shell = [False] * len(stages)
    shell_seen = False
    for index in range(len(stages) - 1, -1, -1):
        downstream_shell[index] = shell_seen
        words = unwrapped[index]
        shell_seen |= bool(words and shell_command_name(words[0]) in _SHELLS)
    shell_source = any(
        words and (_shell_stdin(words) or shell_command_name(words[0]) == "ssh")
        for words in unwrapped
    )
    return HeredocExecution(
        stage_of,
        consumer_executes,
        downstream_shell,
        bool(_OUTPUT_PROCESS_SUBSTITUTION.search(raw)),
        "shell" if shell_source else "language",
        language_stage,
    )
