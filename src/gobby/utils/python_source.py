"""Parse agent-supplied Python without leaking compiler warnings."""

from __future__ import annotations

import ast
import threading
import warnings

# Without ``sys.flags.context_aware_warnings`` (off on the default 3.14 build),
# ``catch_warnings`` swaps the process-wide filter list. Two worker threads
# interleaving enter/exit could leave the ignore filter installed for good, so
# every quiet parse holds this lock for its whole filter window.
_QUIET_PARSE_LOCK = threading.Lock()


def parse_agent_source(source: str) -> ast.Module:
    """``ast.parse(source)`` with the source's SyntaxWarnings dropped.

    Agent-supplied source (for example ``"\\$"`` from shell heredocs) makes the
    compiler emit SyntaxWarnings that would print raw to daemon stderr. They
    describe the agent's text, not daemon code. SyntaxError still propagates.
    """
    with _QUIET_PARSE_LOCK, warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=SyntaxWarning)
        return ast.parse(source)
