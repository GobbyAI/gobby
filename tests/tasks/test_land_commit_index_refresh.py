"""A landing refreshes the main checkout's code index for the paths it landed (gobby-#24973).

The main checkout is always dirty, and the post-merge hook indexes dirty files
instead of the landed ones, so land_candidate refreshes the index itself through
the gcode gateway's per-file flush before it returns.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.code_index.context import CodeIndexContext
from gobby.code_index.eligibility import overlay_project_id_for_root
from gobby.code_index.gcode_gateway import GcodeCommandResult, GcodeGateway
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.close_receipts import LANDING, list_close_receipts
from tests.fixtures.landing_case import LandingCase, make_landing_case

pytestmark = pytest.mark.unit


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


class _Refreshes:
    """Records each incremental index call and its env; optionally fails it."""

    def __init__(self, *, raises: bool = False, returncode: int = 0) -> None:
        self.calls: list[tuple[Path, set[str]]] = []
        self.envs: list[object] = []
        self._raises = raises
        self._returncode = returncode

    async def __call__(
        self, _gateway: GcodeGateway, project_root: Path, files: Sequence[str], **kwargs: object
    ) -> GcodeCommandResult:
        self.calls.append((Path(project_root).resolve(), set(files)))
        self.envs.append(kwargs.get("env"))
        if self._raises:
            raise RuntimeError("gcode unavailable")
        return GcodeCommandResult(
            command=("gcode", "index"),
            returncode=self._returncode,
            stdout="",
            stderr="" if self._returncode == 0 else "index failed",
            started_at="2026-10-25T00:00:00+00:00",
            completed_at="2026-10-25T00:00:01+00:00",
            duration_seconds=1.0,
            timeout_seconds=None,
        )


class _LaunchFactory:
    """A managed launch factory: records each open and yields a launch carrying env."""

    def __init__(
        self, *, env: dict[str, str] | None = None, open_error: Exception | None = None
    ) -> None:
        self.opens: list[tuple[str, str | None]] = []
        self.exits = 0
        self._env = env or {}
        self._open_error = open_error

    @contextmanager
    def open(
        self, project_id: str, *, timeout_seconds: float, code_overlay_project_id: str | None = None
    ) -> Iterator[SimpleNamespace]:
        if self._open_error is not None:
            raise self._open_error
        self.opens.append((project_id, code_overlay_project_id))
        try:
            yield SimpleNamespace(env=self._env)
        finally:
            self.exits += 1


def _code_index(factory: _LaunchFactory | None) -> CodeIndexContext:
    """The daemon's code indexer, carrying the launch factory it binds at startup."""
    return MagicMock(spec=CodeIndexContext, launch_factory=factory)


def _dirty_tracked_file(case: LandingCase) -> None:
    """The shared main checkout always carries other sessions' uncommitted edits."""
    (case.repo / "README.md").write_text("someone else's edit\n")


async def test_fast_forward_landing_refreshes_index_for_landed_paths(case: LandingCase) -> None:
    sha = case.candidate("lane", {"src/landed.py": "landed = 1\n", "docs/landed.md": "doc\n"})
    task = case.reviewed(sha)
    _dirty_tracked_file(case)
    refreshes = _Refreshes()

    with patch.object(
        GcodeGateway, "incremental_index", autospec=True, side_effect=refreshes.__call__
    ):
        result = await case.land(task, sha)

    assert result["landed"] is True, result
    assert result["mode"] == "ff"
    assert refreshes.calls == [(case.repo.resolve(), {"src/landed.py", "docs/landed.md"})]


async def test_merge_landing_refreshes_index_for_candidate_paths(case: LandingCase) -> None:
    sha = case.candidate("lane", {"tests/test_lane.py": "lane = 1\n"})
    case.direct({"docs/moved.md": "moved\n"})
    task = case.reviewed(sha)
    _dirty_tracked_file(case)
    refreshes = _Refreshes()

    with patch.object(
        GcodeGateway, "incremental_index", autospec=True, side_effect=refreshes.__call__
    ):
        result = await case.land(task, sha)

    assert result["landed"] is True, result
    assert result["mode"] == "merge"
    assert refreshes.calls == [(case.repo.resolve(), {"tests/test_lane.py"})]


@pytest.mark.parametrize(
    ("raises", "returncode"), [(True, 0), (False, 2)], ids=["raises", "nonzero-exit"]
)
async def test_index_refresh_failure_is_logged_and_keeps_the_landing(
    case: LandingCase, caplog: pytest.LogCaptureFixture, raises: bool, returncode: int
) -> None:
    sha = case.candidate("lane", {"src/landed.py": "landed = 1\n"})
    task = case.reviewed(sha)
    refreshes = _Refreshes(raises=raises, returncode=returncode)

    with (
        caplog.at_level(logging.WARNING, logger="gobby.tasks.land_commit"),
        patch.object(
            GcodeGateway, "incremental_index", autospec=True, side_effect=refreshes.__call__
        ),
    ):
        result = await case.land(task, sha)

    assert result["landed"] is True, result
    assert "error" not in result
    assert case.git("rev-parse", "trunk") == sha
    landings = [r.commit_sha for r in list_close_receipts(case.db, task.id) if r.kind == LANDING]
    assert landings == [sha]
    assert len(refreshes.calls) == 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("index" in r.getMessage().lower() for r in warnings), caplog.text


async def test_refresh_runs_under_the_managed_launch_env(case: LandingCase) -> None:
    sha = case.candidate("lane", {"src/landed.py": "landed = 1\n"})
    task = case.reviewed(sha)
    refreshes = _Refreshes()
    factory = _LaunchFactory(env={"GOBBY_LAUNCH_GRANT": "granted"})

    with patch.object(
        GcodeGateway, "incremental_index", autospec=True, side_effect=refreshes.__call__
    ):
        result = await case.land(task, sha, code_index=_code_index(factory))

    assert result["landed"] is True, result
    assert factory.opens == [(task.project_id, overlay_project_id_for_root(case.repo))]
    assert factory.exits == 1
    assert refreshes.envs == [{"GOBBY_LAUNCH_GRANT": "granted"}]


async def test_refresh_without_a_launch_factory_runs_unmanaged(case: LandingCase) -> None:
    sha = case.candidate("lane", {"src/landed.py": "landed = 1\n"})
    task = case.reviewed(sha)
    refreshes = _Refreshes()

    with patch.object(
        GcodeGateway, "incremental_index", autospec=True, side_effect=refreshes.__call__
    ):
        result = await case.land(task, sha, code_index=_code_index(None))

    assert result["landed"] is True, result
    assert len(refreshes.calls) == 1
    assert refreshes.envs == [None]


async def test_launch_open_failure_is_logged_and_keeps_the_landing(
    case: LandingCase, caplog: pytest.LogCaptureFixture
) -> None:
    sha = case.candidate("lane", {"src/landed.py": "landed = 1\n"})
    task = case.reviewed(sha)
    refreshes = _Refreshes()
    factory = _LaunchFactory(open_error=ValueError("grant refused"))

    with (
        caplog.at_level(logging.WARNING, logger="gobby.tasks.land_commit"),
        patch.object(
            GcodeGateway, "incremental_index", autospec=True, side_effect=refreshes.__call__
        ),
    ):
        result = await case.land(task, sha, code_index=_code_index(factory))

    assert result["landed"] is True, result
    assert "error" not in result
    assert case.git("rev-parse", "trunk") == sha
    assert refreshes.calls == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("index" in r.getMessage().lower() for r in warnings), caplog.text
