"""Tests for the static test-quality analyzer."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.test_quality import audit_paths
from gobby.test_quality.models import severity_meets_minimum

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


def test_bare_pytest_context_managers_count_as_assertions(tmp_path: Path) -> None:
    codes = _issue_codes(
        tmp_path,
        """
from pytest import deprecated_call, raises, warns


def test_raises():
    with raises(ValueError):
        raise ValueError


def test_warns():
    with warns(UserWarning):
        pass


def test_deprecated_call():
    with deprecated_call():
        pass
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


def test_nested_test_classes_and_unittest_cases_are_analyzed(tmp_path: Path) -> None:
    path = _write_test(
        tmp_path,
        """
import unittest


class TestOuter:
    class TestNested:
        def test_nested(self):
            pass


class LegacyTests(unittest.TestCase):
    def test_legacy(self):
        pass
""",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 2
    assert {issue.test_name for issue in report.issues} == {
        "TestOuter.TestNested.test_nested",
        "LegacyTests.test_legacy",
    }
    assert {issue.issue_code for issue in report.issues} == {"NO_ASSERTION"}


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


def test_bare_xfail_name_and_attribute_match_called_marker_severity(tmp_path: Path) -> None:
    _write_test(
        tmp_path,
        """
import pytest

xfail = pytest.mark.xfail


@xfail
def test_bare_name():
    assert 1 == 1


@pytest.mark.xfail
def test_bare_attribute():
    assert 1 == 1


@pytest.mark.xfail(reason="bug")
def test_called_without_strict():
    assert 1 == 1


@xfail(reason="bug", strict=True)
def test_called_complete():
    assert 1 == 1
""",
    )

    report = audit_paths([tmp_path / "tests"], root=tmp_path)
    xfail_issues = {
        issue.test_name: issue
        for issue in report.issues
        if issue.issue_code == "XFAIL_WITHOUT_STRICT_OR_REASON"
    }

    assert set(xfail_issues) == {
        "test_bare_name",
        "test_bare_attribute",
        "test_called_without_strict",
    }
    called_severity = xfail_issues["test_called_without_strict"].severity
    assert severity_meets_minimum(xfail_issues["test_bare_name"].severity, called_severity)
    assert severity_meets_minimum(xfail_issues["test_bare_attribute"].severity, called_severity)


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


def test_sleep_reports_recognized_timing_apis_and_imported_aliases(tmp_path: Path) -> None:
    _write_test(
        tmp_path,
        """import asyncio
import asyncio as aio
import time
import time as clock
from asyncio import sleep as async_pause
from time import sleep as sync_pause

def test_time_sleep():
    time.sleep(0.01)
    assert True

def test_asyncio_sleep():
    asyncio.sleep(0.01)
    assert True

def test_module_aliases():
    clock.sleep(0.01)
    assert True

def test_asyncio_alias():
    aio.sleep(0.01)
    assert True

def test_imported_time_alias():
    sync_pause(0.01)
    assert True

def test_imported_asyncio_alias():
    async_pause(0.01)
    assert True

def test_local_import_alias():
    from time import sleep as local_pause
    local_pause(0.01)
    assert True

def test_local_alias_does_not_leak():
    local_pause(0.01)
    assert True
""",
    )
    report = audit_paths([tmp_path / "tests"], root=tmp_path)

    sleep_issues = {
        issue.test_name: issue.line
        for issue in report.issues
        if issue.issue_code == "SLEEP_IN_TEST"
    }
    assert sleep_issues == {
        "test_time_sleep": 9,
        "test_asyncio_sleep": 13,
        "test_module_aliases": 17,
        "test_asyncio_alias": 21,
        "test_imported_time_alias": 25,
        "test_imported_asyncio_alias": 29,
        "test_local_import_alias": 34,
    }


def test_sleep_ignores_unrecognized_methods_and_bare_functions(tmp_path: Path) -> None:
    codes = _issue_codes(
        tmp_path,
        """
def test_domain_sleep(client):
    client.sleep()
    sleep()
    assert True
""",
    )

    assert "SLEEP_IN_TEST" not in codes


def test_sleep_suppression_still_applies_to_recognized_alias(tmp_path: Path) -> None:
    codes = _issue_codes(
        tmp_path,
        """from time import sleep as pause

def test_pauses():
    # test-quality: allow SLEEP_IN_TEST -- timing is the behavior under test
    pause(0.01)
    assert True
""",
    )

    assert "SLEEP_IN_TEST" not in codes


def test_class_decorator_does_not_extend_suppression_across_sibling_tests(
    tmp_path: Path,
) -> None:
    _write_test(
        tmp_path,
        """
import pytest


@pytest.mark.usefixtures("resource")
class TestDecorated:
    def test_suppressed(self):
        # test-quality: allow NO_ASSERTION -- verifies cleanup behavior
        cleanup()

    def test_unsuppressed(self):
        exercise()
""",
    )

    report = audit_paths([tmp_path / "tests"], root=tmp_path)

    assert [issue.test_name for issue in report.issues if issue.issue_code == "NO_ASSERTION"] == [
        "TestDecorated.test_unsuppressed"
    ]


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


def test_script_chained_modifiers_and_todo_declarations(tmp_path: Path) -> None:
    tests_dir = tmp_path / "web" / "src" / "__tests__"
    tests_dir.mkdir(parents=True)
    path = tests_dir / "sample.test.ts"
    path.write_text(
        """
import { it, test, expect } from 'vitest'

it.concurrent.skip("skipped", () => {
  expect(true).toBe(true)
})
it.todo("it todo")
test.todo("test todo")
test.fixme("test fixme")
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 4
    codes = [issue.issue_code for issue in report.issues]
    assert codes.count("UNCONDITIONAL_SKIP") == 1
    assert codes.count("TODO_IN_TEST") == 3
    assert "NO_ASSERTION" not in codes


def test_script_config_and_hook_calls_are_not_tests(tmp_path: Path) -> None:
    tests_dir = tmp_path / "web" / "tests"
    tests_dir.mkdir(parents=True)
    path = tests_dir / "sample.spec.ts"
    path.write_text(
        """
import { expect, test } from '@playwright/test'

test.use({ hasTouch: true })
test.setTimeout(30_000)

test.beforeEach(async ({ page }) => {
  await page.goto('/')
})

test.afterAll(() => {
  cleanup()
})

test("clicks land", async ({ page }) => {
  await expect(page.locator('input')).toBeFocused()
})
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 1
    assert report.issues == ()


def test_script_sleep_separates_a_runner_timeout_from_a_bare_timer(tmp_path: Path) -> None:
    tests_dir = tmp_path / "web" / "tests"
    tests_dir.mkdir(parents=True)
    path = tests_dir / "sample.spec.ts"
    path.write_text(
        """
import { expect, test } from '@playwright/test'

test("budgets a slow render", async ({ page }) => {
  test.setTimeout(900_000)
  await page.setDefaultTimeout(60_000)
  await expect(page.locator('.term')).toBeVisible()
})

test("waits by sleeping", async ({ page }) => {
  await new Promise((resolve) => setTimeout(resolve, 250))
  await expect(page.locator('.term')).toBeVisible()
})

test("polls on an interval", async ({ page }) => {
  setInterval(() => page.reload(), 100)
  await expect(page.locator('.term')).toBeVisible()
})
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 3
    sleepers = {issue.test_name for issue in report.issues if issue.issue_code == "SLEEP_IN_TEST"}
    assert sleepers == {"waits by sleeping", "polls on an interval"}


def test_script_delimiter_scanner_ignores_apostrophes_in_comments(tmp_path: Path) -> None:
    tests_dir = tmp_path / "web" / "src" / "__tests__"
    tests_dir.mkdir(parents=True)
    path = tests_dir / "sample.test.ts"
    path.write_text(
        """
import { it, expect } from 'vitest'

it("handles line comments", () => {
  // don't treat this apostrophe or }) as syntax
  expect(true).toBe(true)
})

it("handles block comments", () => {
  /* it's still a comment, even with }) inside */
  expect(true).toBe(true)
})
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 2
    assert report.issues == ()


def test_non_declaration_test_tokens_are_not_test_declarations(tmp_path: Path) -> None:
    """Member access and identifiers containing `test` are not test calls."""
    tests_dir = tmp_path / "web" / "src" / "__tests__"
    tests_dir.mkdir(parents=True)
    path = tests_dir / "sample.test.ts"
    path.write_text(
        r"""
import { it, expect } from 'vitest'

/**
 * The conflict contract lives in WikiPageEditor.conflict.test.tsx (3.2.4).
 */
const matcher = { test: (value) => value === "123" }
const contest = (value) => value === "123"
const $test = (value) => value === "123"
const REGEX_MATCH = /\d+/.test("123")
const MEMBER_MATCH = matcher.test("123")
const WORD_MATCH = contest("123")
const DOLLAR_MATCH = $test("123")

it("uses a regex", () => {
  expect([REGEX_MATCH, MEMBER_MATCH, WORD_MATCH, DOLLAR_MATCH]).toEqual([
    true,
    true,
    true,
    true,
  ])
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


def test_discovery_prunes_excluded_directories_before_descent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    descended_into: list[str] = []

    def walk(directory: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
        dirnames = ["node_modules", "target", "dist", "build", ".git", "ordinary"]
        yield directory, dirnames, []
        descended_into.extend(dirnames)

    monkeypatch.setattr(Path, "walk", walk)

    audit_paths([tests_dir], root=tmp_path)

    assert descended_into == ["build", "ordinary"]


@pytest.mark.parametrize(
    "directory_name",
    [
        ".git",
        "dist",
        "node_modules",
        "target",
    ],
)
def test_discovery_prunes_excluded_directory_components(
    tmp_path: Path,
    directory_name: str,
) -> None:
    tests_dir = tmp_path / "tests"
    excluded_tests_dir = tests_dir / directory_name / "__tests__"
    excluded_tests_dir.mkdir(parents=True)
    (tests_dir / "test_visible.py").write_text(
        "def test_visible():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    (excluded_tests_dir / "test_hidden.py").write_text(
        "def test_hidden():\n    pass\n",
        encoding="utf-8",
    )

    report = audit_paths([tests_dir], root=tmp_path)

    assert report.files_scanned == 1
    assert report.tests_scanned == 1
    assert report.issues == ()


def test_discovery_analyzes_explicit_test_file_in_build_package(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "unit" / "build" / "test_visible.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_visible():\n    assert 1 == 1\n",
        encoding="utf-8",
    )

    report = audit_paths([test_file], root=tmp_path)

    assert report.files_scanned == 1
    assert report.tests_scanned == 1
    assert report.warnings == ()


def test_discovery_walks_build_packages_within_test_tree(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    for relative_directory in (Path("build"), Path("unit/build")):
        test_file = tests_dir / relative_directory / "test_visible.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text(
            "def test_visible():\n    assert 1 == 1\n",
            encoding="utf-8",
        )

    report = audit_paths([tests_dir], root=tmp_path)

    assert report.files_scanned == 2
    assert report.tests_scanned == 2
    assert report.warnings == ()


def test_discovery_excludes_generated_build_output(tmp_path: Path) -> None:
    build_test = tmp_path / "build" / "test_generated.py"
    build_test.parent.mkdir()
    build_test.write_text(
        "def test_generated():\n    assert 1 == 1\n",
        encoding="utf-8",
    )

    for requested_path in (build_test, tmp_path):
        report = audit_paths([requested_path], root=tmp_path)

        assert report.files_scanned == 0
        assert report.tests_scanned == 0
        assert [warning.code for warning in report.warnings] == ["NO_ANALYZABLE_FILES"]


@pytest.mark.parametrize(
    "file_name",
    [
        ".git.py",
        "build.py",
        "dist.py",
        "node_modules.py",
        "target.py",
    ],
)
def test_discovery_keeps_files_named_like_excluded_directories(
    tmp_path: Path,
    file_name: str,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / file_name).write_text(
        "def test_visible():\n    assert 1 == 1\n",
        encoding="utf-8",
    )

    report = audit_paths([tests_dir], root=tmp_path)

    assert report.files_scanned == 1
    assert report.tests_scanned == 1
    assert report.issues == ()


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


def test_rust_multiline_attrs_are_supported(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    path = tests_dir / "sample_test.rs"
    path.write_text(
        """
#[tokio::test(
    flavor = "multi_thread",
    worker_threads = 2,
)]
async fn test_multiline_tokio_attr() {
    assert_eq!(1, 1);
}

#[ignore(
    = "tracked externally"
)]
#[test]
fn test_multiline_ignore_attr() {
    assert_eq!(1, 1);
}
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 2
    assert {issue.issue_code for issue in report.issues} == {"UNCONDITIONAL_SKIP"}
    assert report.issues[0].line == 10


def test_rust_same_line_attrs_and_functions_are_scanned(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    path = tests_dir / "sample_test.rs"
    path.write_text(
        """
#[test] fn test_same_line_without_assertion() { build_value(); }
#[tokio::test] async fn test_same_line_with_assertion() { assert_eq!(1, 1); }
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 2
    assert [(issue.test_name, issue.issue_code) for issue in report.issues] == [
        ("test_same_line_without_assertion", "NO_ASSERTION")
    ]


def test_rust_result_requires_real_try_propagation(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    path = tests_dir / "sample_test.rs"
    path.write_text(
        """
#[test]
fn test_real_try() -> Result<(), Error> {
    load()?;
    Ok(())
}

#[test]
fn test_question_marks_in_text() -> Result<(), Error> {
    let message = "is this enough?";
    // load()? is only an example
    Ok(())
}

#[test]
fn test_non_propagation_question_marks() -> Result<(), Error> {
    fn accepts_unsized<T: ?Sized>(value: &T) {}
    macro_rules! optional { ($($value:expr),?) => {}; }
    Ok(())
}
""",
        encoding="utf-8",
    )

    report = audit_paths([path], root=tmp_path)

    assert report.tests_scanned == 3
    assert [issue.test_name for issue in report.issues if issue.issue_code == "NO_ASSERTION"] == [
        "test_non_propagation_question_marks",
        "test_question_marks_in_text",
    ]


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


@pytest.mark.parametrize(
    ("bad_name", "bad_contents"),
    [
        ("test_bad_syntax.py", "def test_broken(:\n"),
        ("test_bad_encoding.py", b"def test_broken():\n    # \xff\n"),
    ],
)
def test_parse_error_warns_and_audit_continues(
    tmp_path: Path, bad_name: str, bad_contents: str | bytes
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    valid_path = tests_dir / "test_valid.py"
    valid_path.write_text("def test_valid():\n    pass\n", encoding="utf-8")
    bad_path = tests_dir / bad_name
    if isinstance(bad_contents, bytes):
        bad_path.write_bytes(bad_contents)
    else:
        bad_path.write_text(bad_contents, encoding="utf-8")

    report = audit_paths([tests_dir], root=tmp_path)

    assert report.files_scanned == 2
    assert report.tests_scanned == 1
    assert [issue.issue_code for issue in report.issues] == ["NO_ASSERTION"]
    assert [(warning.code, warning.path) for warning in report.warnings] == [
        ("PARSE_ERROR", f"tests/{bad_name}")
    ]


def test_read_error_warns_and_audit_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    valid_path = tests_dir / "test_valid.py"
    valid_path.write_text("def test_valid():\n    assert True\n", encoding="utf-8")
    unreadable_path = tests_dir / "test_unreadable.py"
    unreadable_path.write_text("def test_unreadable():\n    assert True\n", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if path == unreadable_path:
            raise OSError("permission denied")
        return original_read_text(path, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "read_text", read_text)

    report = audit_paths([tests_dir], root=tmp_path)

    assert report.files_scanned == 2
    assert report.tests_scanned == 1
    assert [(warning.code, warning.path) for warning in report.warnings] == [
        ("PARSE_ERROR", "tests/test_unreadable.py")
    ]


@pytest.mark.parametrize("requested_path", ["tests/helper.ts", "tests"])
def test_zero_file_audit_warns_for_unmatched_paths(tmp_path: Path, requested_path: str) -> None:
    path = tmp_path / requested_path
    if path.suffix:
        path.parent.mkdir()
        path.write_text("export const helper = true;\n", encoding="utf-8")

    report = audit_paths([path], root=tmp_path)

    assert report.files_scanned == 0
    assert [(warning.code, warning.path) for warning in report.warnings] == [
        ("NO_ANALYZABLE_FILES", None)
    ]
