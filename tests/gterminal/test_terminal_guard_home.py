"""The terminal fixture protects the operator host despite pytest home isolation."""

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from gobby.paths import get_gobby_home
from tests.terminals import conftest as terminal_fixtures

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("change_home", [False, True])
def test_terminal_fixture_protects_operator_home_after_pytest_isolation(
    change_home: bool,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_home = Path.home() / ".gobby"
    assert get_gobby_home().resolve() != production_home.resolve()
    if change_home:
        monkeypatch.setenv("HOME", str(tmp_path / "test-home"))

    snapshots = iter([{83: production_home / "terminals" / "gterm"}, {}, {}])
    monkeypatch.setattr(terminal_fixtures, "_gterm_hosts", lambda: next(snapshots))

    # Exercise the actual autouse fixture without creating or ending a real host.
    fixture_body = cast(
        Callable[[pytest.TempPathFactory], Iterator[None]],
        inspect.unwrap(terminal_fixtures._assert_no_leaked_hosts),
    )
    guard = fixture_body(tmp_path_factory)
    next(guard)
    with pytest.raises(AssertionError, match="ended durable gterm"):
        next(guard, None)
