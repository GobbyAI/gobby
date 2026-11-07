"""Handoff-only isolated runner; the admitted composer runner stays unchanged."""

import os
from pathlib import Path

from tests.e2e.composer_proof_runner import main as composer_main
from tests.e2e.handoff_continuation_barrier import install_barrier


def main() -> None:
    socket = os.environ.get("GOBBY_HANDOFF_BARRIER_SOCKET")
    if socket is None:
        composer_main()
    else:
        with install_barrier(Path(socket)):
            composer_main()


if __name__ == "__main__":
    main()
