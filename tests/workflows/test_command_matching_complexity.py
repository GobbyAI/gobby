"""Count copied elements rather than relying on machine-dependent timings."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from dataclasses import dataclass
from typing import SupportsIndex, TypeVar, overload

import pytest

from gobby.hooks import _shell_execution_words, provider_launch_guard
from gobby.hooks._ansi_c import ShellDialect
from gobby.hooks._normalization_shell import (
    HeredocBody,
    ShellScan,
    ShellToken,
    _PendingHeredoc,
    _skip_heredoc_bodies,
    scan_shell_command,
)
from gobby.hooks._shell_execution_words import _unwrap
from gobby.workflows.engine import command_matching
from gobby.workflows.engine.command_matching import _subjects, _wrapper_scripts

pytestmark = pytest.mark.unit
_T = TypeVar("_T")


@dataclass
class _Copies:
    count: int = 0


class _CountedWords(list[str]):
    def __init__(self, words: list[str], copies: _Copies) -> None:
        super().__init__(words)
        self.copies = copies

    @overload
    def __getitem__(self, key: SupportsIndex) -> str: ...

    @overload
    def __getitem__(self, key: slice) -> _CountedWords: ...

    def __getitem__(self, key: SupportsIndex | slice) -> str | _CountedWords:
        result = super().__getitem__(key)
        if isinstance(key, slice):
            assert isinstance(result, list)
            self.copies.count += len(result)
            return _CountedWords(result, self.copies)
        assert isinstance(result, str)
        return result


class _CountedText(str):
    copies: _Copies

    def __new__(cls, text: str, copies: _Copies) -> _CountedText:
        instance = super().__new__(cls, text)
        instance.copies = copies
        return instance

    def __getitem__(self, key: SupportsIndex | slice) -> str:
        result = super().__getitem__(key)
        if isinstance(key, slice):
            self.copies.count += len(result)
        return result


@pytest.mark.parametrize("size", [1000, 2000])
def test_literal_wrapper_tail_copies_grow_linearly(size: int) -> None:
    copies = _Copies()
    words = _CountedWords(["env"] * size + ["true"], copies)
    assert _unwrap(words) == ["true"]
    assert copies.count <= 4 * len(words)


@pytest.mark.parametrize("size", [1000, 2000])
def test_adjacent_substitution_source_copies_grow_linearly(size: int) -> None:
    copies = _Copies()
    command = _CountedText('echo "' + "$(true)" * size + '"', copies)
    subjects = _subjects(command, 0)
    assert "true" in subjects
    assert copies.count <= 20 * len(command)


@pytest.mark.parametrize("size", [1000, 2000])
def test_find_expression_tail_copies_grow_linearly(size: int) -> None:
    copies = _Copies()
    words = _CountedWords(["find", ".", *["-exec", "true", ";"] * size], copies)
    assert _wrapper_scripts([words]) == ["true"] * size
    assert copies.count <= 4 * len(words)


@pytest.mark.parametrize("size", [1000, 2000])
def test_env_split_option_tail_copies_grow_linearly(
    size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    copies = _Copies()
    split_string = _shell_execution_words._env_split_string

    def counted_split(
        option: str, words: list[str], value_options: Collection[str], *, start: int = 0
    ) -> list[str] | None:
        result = split_string(option, words, value_options, start=start)
        if result is not None:
            copies.count += len(result)
        return result

    monkeypatch.setattr(_shell_execution_words, "_env_split_string", counted_split)
    words = ["env", *["-S", "-i"] * size, "echo"]
    assert _unwrap(words) == ["echo"]
    assert copies.count <= 4 * len(words)


class _Visited(list[_T]):
    def __init__(self, values: list[_T], visits: _Copies) -> None:
        super().__init__(values)
        self.visits = visits

    def __iter__(self) -> Iterator[_T]:
        for value in super().__iter__():
            self.visits.count += 1
            yield value


@pytest.mark.parametrize("size", [100, 200])
def test_heredoc_segment_visits_grow_linearly(size: int, monkeypatch: pytest.MonkeyPatch) -> None:
    visits = _Copies()
    command = "cat <<'E'\nx\nE\n" * size
    scan_command = scan_shell_command

    def counted_scan(text: str) -> ShellScan:
        scan = scan_command(text)
        if text == command:
            return ShellScan(scan.tokens, scan.spans, _Visited(scan.heredocs, visits))
        return scan

    monkeypatch.setattr(command_matching, "scan_shell_command", counted_scan)
    subjects = _subjects(command, 0)
    assert len(subjects) == size
    assert all(subject.strip() == "cat <<'E'" for subject in subjects)
    assert visits.count <= 8 * size


@pytest.mark.parametrize("size", [100, 200])
@pytest.mark.parametrize("pipeline", [False, True], ids=["data", "downstream-shell"])
def test_many_heredoc_stage_visits_grow_linearly(
    size: int, pipeline: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    visits = _Copies()
    command = "cat" + " <<'E'" * size + (" | sh" if pipeline else "") + "\n" + "x\nE\n" * size
    split_stages = command_matching._pipeline_stages

    def counted_stages(tokens: list[ShellToken]) -> list[list[ShellToken]]:
        visits.count += len(tokens)
        return split_stages(tokens)

    monkeypatch.setattr(command_matching, "_pipeline_stages", counted_stages)
    subjects = _subjects(command, 0)
    assert len(subjects) == 1 + (size if pipeline else 0)
    assert visits.count <= 12 * len(scan_shell_command(command).tokens)


class _CountedQueue(list[_PendingHeredoc]):
    def __init__(self, values: list[_PendingHeredoc], shifts: _Copies) -> None:
        super().__init__(values)
        self.shifts = shifts

    def pop(self, index: SupportsIndex = -1) -> _PendingHeredoc:
        position = index.__index__()
        if position < 0:
            position += len(self)
        self.shifts.count += len(self) - position - 1
        return super().pop(index)


@pytest.mark.parametrize("size", [1000, 2000])
def test_pending_heredoc_queue_shifts_grow_linearly(size: int) -> None:
    shifts = _Copies()
    opener = "cat" + " <<E" * size + "\n"
    command = opener + "E\n" * size
    pending = _CountedQueue(
        [_PendingHeredoc("E", False, False, index) for index in range(size)], shifts
    )
    bodies: list[HeredocBody] = []
    assert _skip_heredoc_bodies(command, len(opener), pending, bodies) == len(command)
    assert not pending
    assert len(bodies) == size
    assert all(body.terminated and body.text == "" for body in bodies)
    assert shifts.count <= 4 * size


@pytest.mark.parametrize("size", [1000, 2000])
def test_echo_option_tail_copies_grow_linearly(size: int, monkeypatch: pytest.MonkeyPatch) -> None:
    copies = _Copies()

    def counted_unwrap(words: list[str]) -> list[str]:
        return _CountedWords(_unwrap(words), copies)

    monkeypatch.setattr(command_matching, "_unwrap", counted_unwrap)
    assert set(command_matching._shell_input_scripts("echo " + "-n " * size + "x | sh")) == {"x"}
    assert copies.count <= 8 * (size + 1)


@pytest.mark.parametrize("size", [100, 200])
@pytest.mark.parametrize("entry", ["matcher", "provider"])
@pytest.mark.parametrize("producer", ["cat", "echo x"])
@pytest.mark.parametrize("last", ["cat", "sh"])
def test_downstream_pipeline_stage_visits_grow_linearly(
    size: int, entry: str, producer: str, last: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    visits = _Copies()
    consumes = provider_launch_guard._segment_consumes

    def counted_consumes(
        segment: list[ShellToken],
        expansions: list[str],
        depth: int,
        dialect: ShellDialect,
        *,
        python: bool,
        functions: dict[str, str] | None = None,
    ) -> bool:
        visits.count += 1
        return consumes(segment, expansions, depth, dialect, python=python, functions=functions)

    monkeypatch.setattr(provider_launch_guard, "_segment_consumes", counted_consumes)
    command = (producer + " | ") * size + last
    if entry == "provider":
        assert not provider_launch_guard.blocks_direct_provider_launch("Bash", {"command": command})
    else:
        scripts = command_matching._shell_input_scripts(command)
        assert bool(scripts) is (producer == "echo x" and last == "sh")
    assert visits.count <= 40 * (size + 1)


@pytest.mark.parametrize("size", [100, 200])
def test_provider_heredoc_segment_visits_grow_linearly(
    size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    visits = _Copies()
    command = "cat <<'E'\nx\nE\n" * size

    def counted_scan(text: str, *, dialect: ShellDialect = "bash") -> ShellScan:
        scan = scan_shell_command(text, dialect=dialect)
        if len(scan.heredocs) == size:
            return ShellScan(scan.tokens, scan.spans, _Visited(scan.heredocs, visits))
        return scan

    monkeypatch.setattr(provider_launch_guard, "scan_shell_command", counted_scan)
    assert not provider_launch_guard.blocks_direct_provider_launch("Bash", {"command": command})
    assert visits.count <= 16 * size


@pytest.mark.parametrize("size", [100, 200])
def test_pipeline_function_definition_visits_grow_linearly(
    size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    visits = _Copies()
    consumes = provider_launch_guard._segment_consumes

    def counted_consumes(
        segment: list[ShellToken],
        expansions: list[str],
        depth: int,
        dialect: ShellDialect,
        *,
        python: bool,
        functions: dict[str, str] | None = None,
    ) -> bool:
        visits.count += 1
        return consumes(segment, expansions, depth, dialect, python=python, functions=functions)

    monkeypatch.setattr(provider_launch_guard, "_segment_consumes", counted_consumes)
    command = "f() { cat; } | " * size + "f"
    assert not provider_launch_guard.blocks_direct_provider_launch("Bash", {"command": command})
    assert visits.count <= 40 * (size + 1)


@pytest.mark.parametrize(
    "command",
    [
        "f(){ bash; }; echo 'claude -p hi' | f | f(){ cat; }",
        "echo 'claude -p hi' | sh | sh(){ cat; }",
        "sh(){ cat; } | echo 'claude -p hi' | sh",
    ],
)
def test_pipeline_definitions_cannot_hide_shell_consumers(command: str) -> None:
    assert provider_launch_guard.blocks_direct_provider_launch("Bash", {"command": command})
