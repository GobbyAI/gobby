"""The worker protocol contract pins each command's error codes to the code's sets.

Contract: docs/contracts/worker-protocol.md. Its per-command error-code table is the table
whose header is ``| Command | Error codes |``: one row per command, with that command's codes
in backticks, comma separated. The code's sets are the ``*_ERROR_CODES`` constants in
crates/gdaemon/src/lifecycle/worker_handshake.rs. Python keeps no per-command sets (its
``ErrorCode`` enum is the full wire list), so the code side is read from the Rust text.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "docs/contracts/worker-protocol.md"
RUST_CLIENT = REPO / "crates/gdaemon/src/lifecycle/worker_handshake.rs"
COMMANDS = ("handshake", "initialize", "invoke")
CODE = re.compile(r"`([a-z_]+)`")
STRING_CONST = re.compile(r'const (\w+): &str = "([^"]*)";')
ERROR_SET_CONST = re.compile(
    r"const (HANDSHAKE|INITIALIZE|INVOKE)_ERROR_CODES: \[&str; \d+\] = \[([^\]]*)\];"
)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table_rows(text: str, header: tuple[str, ...]) -> list[list[str]] | None:
    """The body rows of the first markdown table whose header cells are ``header``."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("|") and _cells(line) == list(header):
            rows: list[list[str]] = []
            for row in lines[index + 2 :]:
                if not row.strip().startswith("|"):
                    break
                rows.append(_cells(row))
            return rows
    return None


def _plain(cell: str) -> str:
    return cell.strip("`")


def _code_sets() -> dict[str, set[str]]:
    """Each command's code set, read from the Rust constants.

    An entry is either a quoted code or the name of a ``&str`` constant that holds one.
    """
    text = RUST_CLIENT.read_text(encoding="utf-8")
    names = dict(STRING_CONST.findall(text))
    sets: dict[str, set[str]] = {}
    for name, body in ERROR_SET_CONST.findall(text):
        codes: set[str] = set()
        for part in body.split(","):
            item = part.strip()
            if item:
                codes.add(item.strip('"') if item.startswith('"') else names.get(item, item))
        sets[name.lower()] = codes
    return sets


def test_guard_order_rows_list_each_code_once() -> None:
    rows = _table_rows(CONTRACT.read_text(encoding="utf-8"), ("Step", "Check", "Failure"))
    assert rows, "docs/contracts/worker-protocol.md has no 'Step | Check | Failure' guard table"
    for row in rows:
        codes = CODE.findall(row[2])
        repeated = sorted({code for code in codes if codes.count(code) > 1})
        assert not repeated, f"guard step {row[0]} lists {repeated} more than once"


@pytest.mark.parametrize("command", COMMANDS)
def test_documented_error_codes_equal_the_code_set(command: str) -> None:
    rows = _table_rows(CONTRACT.read_text(encoding="utf-8"), ("Command", "Error codes"))
    assert rows is not None, (
        "docs/contracts/worker-protocol.md has no per-command error-code table; add a table "
        "whose header is '| Command | Error codes |' with one row per command"
    )
    listed = [(_plain(row[0]), CODE.findall(row[1])) for row in rows]
    names = [name for name, _ in listed]
    assert len(names) == len(set(names)), f"the per-command table repeats a row: {names}"
    documented = dict(listed)
    assert command in documented, f"the per-command table has no row for {command}"
    codes = documented[command]
    assert len(codes) == len(set(codes)), f"the {command} row lists a code twice: {codes}"
    expected = _code_sets().get(command)
    assert expected is not None, f"the Rust client has no {command.upper()}_ERROR_CODES set"
    assert set(codes) == expected, (
        f"{command}: the doc lists {sorted(codes)} but the code's set is {sorted(expected)}; "
        f"missing from the doc {sorted(expected - set(codes))}, "
        f"extra in the doc {sorted(set(codes) - expected)}"
    )
