"""Tests for the static test-quality analyzer."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.test_quality import audit_paths

pytestmark = pytest.mark.unit


def _write_test(tmp_path: Path, source: str) -> Path:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(exist_ok=True)
    path = tests_dir / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    return path


def _issue_codes(tmp_path: Path, source: str) -> set[str]:
    _write_test(tmp_path, source)
    report = audit_paths([tmp_path / "tests"], root=tmp_path)
    return {issue.issue_code for issue in report.issues}


def test_async_no_assertion_is_reported(tmp_path: Path) -> None:
    codes = _issue_codes(
        tmp_path,
        """
async def test_background_cleanup():
    await cleanup()
""",
    )

    assert codes == {"NO_ASSERTION"}


def test_parametrized_pytest_raises_counts_as_assertion(tmp_path: Path) -> None:
    codes = _issue_codes(
        tmp_path,
        """
import pytest


@pytest.mark.parametrize("value", [1, 2])
def test_rejects_value(value):
    with pytest.raises(ValueError):
        raise ValueError(value)
""",
    )

    assert codes == set()


def test_assert_true_is_reported(tmp_path: Path) -> None:
    codes = _issue_codes(
        tmp_path,
        """
def test_padding():
    assert True
""",
    )

    assert codes == {"ASSERT_TRUE"}


def test_skip_and_xfail_decorators_are_reported(tmp_path: Path) -> None:
    codes = _issue_codes(
        tmp_path,
        """
import pytest


@pytest.mark.skip(reason="later")
def test_skipped():
    assert 1 == 1


@pytest.mark.xfail(reason="bug")
def test_xfail_without_strict():
    assert 1 == 1


@pytest.mark.xfail(strict=True)
def test_xfail_without_reason():
    assert 1 == 1


@pytest.mark.xfail(reason="bug", strict=True)
def test_xfail_ok():
    assert 1 == 1
""",
    )

    assert codes == {"UNCONDITIONAL_SKIP", "XFAIL_WITHOUT_STRICT_OR_REASON"}


def test_sleep_todo_and_suppression_handling(tmp_path: Path) -> None:
    _write_test(
        tmp_path,
        """
import time


def test_sleepy():
    # TODO: replace timing dependency
    time.sleep(0.01)
    assert 1 == 1


def test_cleanup_no_exception():
    # test-quality: allow NO_ASSERTION -- verifies no exception from cleanup path
    cleanup()
""",
    )
    report = audit_paths([tmp_path / "tests"], root=tmp_path)

    assert {issue.issue_code for issue in report.issues} == {"SLEEP_IN_TEST", "TODO_IN_TEST"}


def test_mock_only_and_heavy_mock_low_assertion_are_reported(tmp_path: Path) -> None:
    _write_test(
        tmp_path,
        """
from unittest.mock import patch


@patch("pkg.one")
@patch("pkg.two")
@patch("pkg.three")
@patch("pkg.four")
def test_only_mock_assertions(one, two, three, four):
    one.assert_called_once()
""",
    )
    report = audit_paths([tmp_path / "tests"], root=tmp_path)

    assert {issue.issue_code for issue in report.issues} == {
        "HEAVY_MOCK_LOW_ASSERT",
        "ONLY_MOCK_ASSERTIONS",
    }
    assert [item.identifier for item in report.ranked_tests] == [
        "tests/test_sample.py::test_only_mock_assertions"
    ]


def test_fingerprints_are_path_test_and_code(tmp_path: Path) -> None:
    _write_test(
        tmp_path,
        """
class TestExample:
    def test_padding(self):
        assert True
""",
    )
    report = audit_paths([tmp_path / "tests"], root=tmp_path)

    assert [issue.fingerprint for issue in report.issues] == [
        "tests/test_sample.py::TestExample.test_padding::ASSERT_TRUE"
    ]
