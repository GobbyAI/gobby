"""Platform fallback for daemon signal handling.

``loop.add_signal_handler`` is Unix-only; Windows event loops raise
``NotImplementedError``. ``setup_signal_handlers`` must fall back to
``signal.signal`` so the daemon can still shut down gracefully.
"""

from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

import pytest

from gobby.runner_maintenance import setup_signal_handlers

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class TestSetupSignalHandlers:
    async def test_uses_loop_handlers_when_supported(self) -> None:
        """On POSIX the asyncio loop registers the handlers directly."""
        loop = MagicMock()

        with patch("asyncio.get_running_loop", return_value=loop):
            setup_signal_handlers(lambda: None)

        registered = {call.args[0] for call in loop.add_signal_handler.call_args_list}
        assert registered == {signal.SIGTERM, signal.SIGINT}
        loop.call_soon_threadsafe.assert_not_called()

    async def test_falls_back_to_signal_signal_on_windows(self) -> None:
        """A loop without add_signal_handler must not abort daemon startup."""
        loop = MagicMock()
        loop.add_signal_handler.side_effect = NotImplementedError

        with (
            patch("asyncio.get_running_loop", return_value=loop),
            patch("signal.signal") as mock_signal,
        ):
            setup_signal_handlers(lambda: None)

        registered = {call.args[0] for call in mock_signal.call_args_list}
        assert registered == {signal.SIGTERM, signal.SIGINT}

    async def test_fallback_handler_marshals_onto_the_loop(self) -> None:
        """The signal.signal handler must hop back onto the loop thread."""
        loop = MagicMock()
        loop.add_signal_handler.side_effect = NotImplementedError
        shutdown_called = False

        def shutdown() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        with (
            patch("asyncio.get_running_loop", return_value=loop),
            patch("signal.signal") as mock_signal,
        ):
            setup_signal_handlers(shutdown)

        # Invoke the handler signal.signal was given, as the OS would.
        handler = mock_signal.call_args_list[0].args[1]
        handler(signal.SIGTERM, None)

        loop.call_soon_threadsafe.assert_called_once()
        # The queued callable is the real shutdown path, not the raw signal handler.
        loop.call_soon_threadsafe.call_args.args[0]()
        assert shutdown_called is True

    async def test_fallback_binds_each_signal_to_its_own_handler(self) -> None:
        """Late binding in the loop must not collapse both signals onto one handler."""
        loop = MagicMock()
        loop.add_signal_handler.side_effect = NotImplementedError

        with (
            patch("asyncio.get_running_loop", return_value=loop),
            patch("signal.signal") as mock_signal,
        ):
            setup_signal_handlers(lambda: None)

        handlers = [call.args[1] for call in mock_signal.call_args_list]
        assert len(handlers) == 2
        assert handlers[0] is not handlers[1]

        # Both OS handlers must marshal distinct callables onto the loop.
        handlers[0](signal.SIGTERM, None)
        handlers[1](signal.SIGINT, None)

        assert loop.call_soon_threadsafe.call_count == 2
        queued = [call.args[0] for call in loop.call_soon_threadsafe.call_args_list]
        assert queued[0] is not queued[1]
