"""gclient's sessions fetch asks for exactly the daemon's live session statuses."""

import re
from pathlib import Path

from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER

ROOT = Path(__file__).resolve().parents[2]
REST = ROOT / "crates/gclient/src/daemon/rest.rs"


def test_gclient_live_statuses_match_daemon_order() -> None:
    source = REST.read_text(encoding="utf-8")
    match = re.search(
        r"const LIVE_SESSION_STATUSES: \[&str; (\d+)\] = \[(.*?)\];", source, re.DOTALL
    )
    assert match is not None, "LIVE_SESSION_STATUSES not found in rest.rs"
    statuses = tuple(re.findall(r'"([a-z_]+)"', match.group(2)))
    assert statuses == LIVE_SESSION_STATUS_ORDER
    assert int(match.group(1)) == len(LIVE_SESSION_STATUS_ORDER)
