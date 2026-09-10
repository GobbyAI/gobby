"""Keep the handoff admission fence held through the CLI's stop operation."""

import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager

from gobby.cli.runtime import CliRuntime
from gobby.sessions.handoff_shutdown import HandoffShutdownBlocked, guard_handoff_shutdown
from gobby.utils.machine_id import require_machine_id


@contextmanager
def protect_pending_handoffs(
    runtime: CliRuntime, *, wait: bool, report: Callable[[str], None]
) -> Iterator[None]:
    db = runtime.require_database(apply_migrations=False)
    machine_id = require_machine_id()
    deadline = time.monotonic() + 600
    announced = False
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(guard_handoff_shutdown(db, machine_id))
                break
            except HandoffShutdownBlocked as exc:
                if not wait or time.monotonic() >= deadline:
                    raise
                if not announced:
                    report(f"Waiting for handoff completion: {exc}")
                    announced = True
                time.sleep(1)
        yield
