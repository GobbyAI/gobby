"""Tests for stripping the outer alternate-screen switch from a tmux stream."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.agents.pty_reader import PTYReaderManager
from gobby.agents.tmux.alt_screen import ALT_SCREEN_SWITCHES, AltScreenFilter

pytestmark = pytest.mark.unit


class TestWholeChunks:
    @pytest.mark.parametrize("switch", ALT_SCREEN_SWITCHES)
    def test_every_switch_spelling_is_removed(self, switch: str) -> None:
        assert AltScreenFilter()(f"before{switch}after") == "beforeafter"

    def test_the_real_tmux_attach_preamble_keeps_everything_else(self) -> None:
        # Captured from `tmux -L gobby -T 256,RGB attach-session`: smcup is
        # the very first thing on the wire, followed by the repaint the
        # client-side screen pad already accounts for.
        raw = "\x1b[?1049h\x1b[22;0;0t\x1b[?1h\x1b=\x1b[H\x1b[2Jhello"

        assert AltScreenFilter()(raw) == "\x1b[22;0;0t\x1b[?1h\x1b=\x1b[H\x1b[2Jhello"

    def test_repeated_switches_in_one_chunk_all_go(self) -> None:
        raw = "\x1b[?1049ha\x1b[?1049lb\x1b[?1049hc"

        assert AltScreenFilter()(raw) == "abc"

    def test_plain_text_is_returned_unchanged(self) -> None:
        assert AltScreenFilter()("no escapes here") == "no escapes here"

    def test_empty_chunk_stays_empty(self) -> None:
        assert AltScreenFilter()("") == ""


class TestNeighbouringSequences:
    @pytest.mark.parametrize(
        "sequence",
        [
            "\x1b[?1000h",  # mouse reporting, shares the CSI ? prefix
            "\x1b[?1048h",  # save cursor, one digit away from 1049
            "\x1b[?104h",  # a prefix of the switch, not the switch
            "\x1b[?10490h",  # the switch's digits with one more appended
            "\x1b[?470h",
            "\x1b[?1049m",  # right parameters, wrong final byte
            "\x1b[H\x1b[2J",
        ],
    )
    def test_lookalike_sequences_pass_through(self, sequence: str) -> None:
        assert AltScreenFilter()(f"a{sequence}b") == f"a{sequence}b"

    def test_a_switch_directly_behind_another_escape_is_still_found(self) -> None:
        # Forwarding an unmatched ESC must resume scanning at the next
        # character rather than skipping the sequence that follows it.
        assert AltScreenFilter()("\x1b[2J\x1b[?1049hx") == "\x1b[2Jx"


class TestChunkBoundaries:
    @pytest.mark.parametrize("split", range(1, len("\x1b[?1049h")))
    def test_a_switch_split_anywhere_is_still_removed(self, split: int) -> None:
        switch = "\x1b[?1049h"
        alt_screen = AltScreenFilter()

        first = alt_screen("head" + switch[:split])
        second = alt_screen(switch[split:] + "tail")

        assert first + second == "headtail"

    def test_a_switch_split_one_character_per_chunk_is_removed(self) -> None:
        alt_screen = AltScreenFilter()

        emitted = "".join(alt_screen(char) for char in "a\x1b[?1049hb")

        assert emitted == "ab"

    def test_a_withheld_fragment_that_is_not_a_switch_is_emitted_in_order(self) -> None:
        alt_screen = AltScreenFilter()

        first = alt_screen("head\x1b[?10")
        second = alt_screen("00htail")

        assert first == "head"
        assert first + second == "head\x1b[?1000htail"

    def test_a_trailing_bare_escape_is_withheld_then_released(self) -> None:
        alt_screen = AltScreenFilter()

        assert alt_screen("text\x1b") == "text"
        assert alt_screen("[2J") == "\x1b[2J"

    def test_an_escape_that_cannot_start_a_switch_is_released_immediately(self) -> None:
        # ESC ] opens an OSC, which no switch begins with, so nothing is held
        # back waiting for bytes that would never make it one.
        alt_screen = AltScreenFilter()

        assert alt_screen("\x1b]0;title") == "\x1b]0;title"

    def test_a_stream_ending_mid_switch_drops_the_incomplete_fragment(self) -> None:
        # An escape sequence that never completed has nothing to render, and
        # the attachment is over by the time this matters.
        alt_screen = AltScreenFilter()

        assert alt_screen("done\x1b[?1049") == "done"

    def test_instances_do_not_share_carry_state(self) -> None:
        first = AltScreenFilter()
        second = AltScreenFilter()

        first("\x1b[?10")

        assert second("49h") == "49h"


class TestReaderWiring:
    """The filter only matters if the PTY reader actually applies it."""

    @staticmethod
    async def _stream(payload: list[bytes], *, filtered: bool) -> str:
        received: list[str] = []

        async def collect(run_id: str, data: str) -> None:
            del run_id
            received.append(data)

        master_fd, slave_fd = os.openpty()
        manager = PTYReaderManager(collect)
        agent = SimpleNamespace(run_id="stream-under-test", master_fd=master_fd)

        started = await manager.start_reader(
            cast(Any, agent),
            transform=AltScreenFilter() if filtered else None,
        )
        assert started is True
        try:
            for chunk in payload:
                os.write(slave_fd, chunk)
                # The reader polls the fd; wait for the chunk it produced
                # rather than for a fixed delay.
                deadline = asyncio.get_running_loop().time() + 5
                seen = len(received)
                while len(received) == seen:
                    if asyncio.get_running_loop().time() > deadline:
                        raise AssertionError("reader produced no output for chunk")
                    await asyncio.sleep(0.01)
        finally:
            await manager.stop_reader("stream-under-test")
            os.close(slave_fd)
            os.close(master_fd)

        return "".join(received)

    @pytest.mark.asyncio
    async def test_an_opted_in_stream_loses_the_switch_across_a_chunk_boundary(
        self,
    ) -> None:
        emitted = await self._stream([b"head\x1b[?10", b"49htail"], filtered=True)

        assert emitted == "headtail"

    @pytest.mark.asyncio
    async def test_a_stream_without_a_transform_is_untouched(self) -> None:
        # Agent PTYs share this manager and must keep byte-for-byte fidelity.
        emitted = await self._stream([b"head\x1b[?1049htail"], filtered=False)

        assert emitted == "head\x1b[?1049htail"
