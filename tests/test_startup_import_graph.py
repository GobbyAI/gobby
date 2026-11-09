"""The daemon's startup import graph must not load optional vector-store clients.

Startup cost grows with the number of modules imported before the health
endpoint binds, and under host load each import stalls. The check reads which
modules are loaded, so it does not depend on wall time.
"""

from __future__ import annotations

import json
import subprocess
import sys

_PROBE = """
import importlib
import json
import sys

importlib.import_module("gobby.servers.app_factory")
print(json.dumps(sorted(sys.modules)))
"""

# The daemon entry graph reaches the memory vector store, so that module must load;
# the client stack it wraps must stay unloaded until a store is actually used.
_CONTROL_MODULE = "gobby.memory.vectorstore"
_DEFERRED_PACKAGES = ("qdrant_client",)


def test_daemon_startup_graph_defers_vector_store_clients() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    loaded = set(json.loads(completed.stdout.splitlines()[-1]))

    assert _CONTROL_MODULE in loaded, "probe did not reach the memory vector store"
    eager = sorted(name for name in loaded if name.split(".")[0] in _DEFERRED_PACKAGES)
    assert eager == [], f"startup imported vector-store clients eagerly: {eager}"
