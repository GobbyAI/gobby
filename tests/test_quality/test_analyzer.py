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


def test_private_assertion_helper_counts_as_assertion(tmp_path: Path) -> None:
    codes = _issue_codes(
        tmp_path,
        """
def _assert_payload(payload):
    assert payload == {"ok": True}


def test_uses_private_assertion_helper():
    _assert_payload({"ok": True})
""",
    )

    assert codes == set()


def test_fixture_named_like_test_is_not_analyzed(tmp_path: Path) -> None:
    codes = _issue_codes(
        tmp_path,
        """
import pytest
from pytest import fixture


@pytest.fixture
def test_project():
    return object()


@fixture()
def test_db():
    return object()


def test_uses_fixture(test_project):
    assert test_project is not None
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


def test_script_test_name_parser_handles_escaped_quotes(tmp_path: Path) -> None:
    tests_dir = tmp_path / "web" / "src" / "__tests__"
    tests_dir.mkdir(parents=True)
    path = tests_dir / "sample.test.ts"
    path.write_text(
        """
import { it, expect } from 'vitest'

it("handles \\"quoted\\" names", () => {
  expect(true).toBe(true)
})
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 1
    assert report.issues == ()


def test_supported_test_suffixes_are_analyzed_without_unsupported_warnings(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    files = {
        "test_sample.py": "def test_python():\n    assert 1 == 1\n",
        "sample_test.rs": "#[test]\nfn test_rust() {\n    assert_eq!(1, 1);\n}\n",
        "sample.test.cjs": 'test("cjs", () => {\n  expect(true).toBe(true);\n});\n',
        "sample.test.js": 'test("js", () => {\n  expect(true).toBe(true);\n});\n',
        "sample.test.mjs": 'test("mjs", () => {\n  expect(true).toBe(true);\n});\n',
        "sample.test.ts": 'test("ts", () => {\n  expect(true).toBe(true);\n});\n',
        "sample.test.cts": 'test("cts", () => {\n  expect(true).toBe(true);\n});\n',
        "sample.test.mts": 'test("mts", () => {\n  expect(true).toBe(true);\n});\n',
        "sample.test.tsx": 'test("tsx", () => {\n  expect(true).toBe(true);\n});\n',
    }
    for file_name, source in files.items():
        (tests_dir / file_name).write_text(source, encoding="utf-8")

    report = audit_paths([tests_dir], root=tmp_path)

    assert report.files_scanned == 9
    assert report.tests_scanned == 9
    assert report.warnings == ()


def test_rust_assertion_like_checks_are_supported(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    path = tests_dir / "sample_test.rs"
    path.write_text(
        """
#[test]
fn test_assert_eq() {
    let s: &'static str = "x";
    let c: char = 'x';
    assert_eq!(1, 1);
    assert_eq!(s, "x");
    assert_eq!(c, 'x');
}

#[tokio::test]
async fn test_result_path() -> Result<(), anyhow::Error> {
    load().await?;
    Ok(())
}

#[rstest]
#[case(1)]
fn test_rstest_case(#[case] value: i32) {
    assert!(matches!(value, 1));
}

#[test_case(1)]
fn test_case_macro(value: i32) {
    insta::assert_debug_snapshot!(value);
}

#[test]
#[should_panic(expected = "boom")]
fn test_expected_panic() {
    panic!("boom");
}

#[quickcheck]
fn quickcheck_accepts_property(value: bool) -> bool {
    value || !value
}

proptest! {
    #[test]
    fn prop_never_crashes(value in 0u8..) {
        prop_assert!(value <= 255);
    }
}
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 7
    assert report.issues == ()


def test_rust_problem_patterns_are_reported(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    path = tests_dir / "sample_test.rs"
    path.write_text(
        """
#[test]
fn test_no_assertion() {
    build_value();
}

#[ignore]
#[test]
fn test_ignored() {
    assert_eq!(1, 1);
}

#[test]
fn test_trivial() {
    assert!(true);
}

#[test]
fn test_sleep() {
    std::thread::sleep(Duration::from_millis(1));
    assert_eq!(1, 1);
}
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert {issue.issue_code for issue in report.issues} == {
        "ASSERT_TRUE",
        "NO_ASSERTION",
        "SLEEP_IN_TEST",
        "UNCONDITIONAL_SKIP",
    }


def test_unsupported_test_file_warns_without_issues(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    path = tests_dir / "user_test.go"
    path.write_text(
        """
package tests

func TestUser(t *testing.T) {
    t.Fatal("native validation owns this language")
}
""",
        encoding="utf-8",
    )

    report = audit_paths([tests_dir], root=tmp_path)

    assert report.files_scanned == 0
    assert report.tests_scanned == 0
    assert report.issues == ()
    assert [warning.code for warning in report.warnings] == ["UNSUPPORTED_LANGUAGE"]
    assert "audit attempted but unsupported" in report.warnings[0].message
