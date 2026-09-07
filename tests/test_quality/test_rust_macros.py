"""Rust macro-emitted tests must participate in the public audit pipeline."""

from pathlib import Path

import pytest

from gobby.test_quality.analyzer import analyze_file, audit_paths

pytestmark = pytest.mark.unit

TEST_MACRO = """
macro_rules! cases {
    ($(fn $name:ident() $body:block)*) => {
        $(#[test] fn $name() $body)*
    };
}
"""


def _write(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_macro_emitted_functions_are_audited_with_source_locations(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", '[package]\nname = "sample"\nversion = "0.1.0"\n')
    _write(tmp_path / "tests/parity/mod.rs", TEST_MACRO)
    source = """cases! {
    fn checked() { assert_eq!(actual(), expected()); }
    fn empty() {}
    #[ignore]
    fn disabled() { assert!(true); }
}
#[test]
fn plain() { assert_eq!(actual(), expected()); }
"""
    path = _write(tmp_path / "tests/parity/cases.rs", source)

    # Definitions outside the requested file still belong to its crate.
    report = audit_paths([path], root=tmp_path)
    assert report.files_scanned == 1
    assert report.tests_scanned == 4
    assert {(issue.test_name, issue.issue_code, issue.line) for issue in report.issues} == {
        ("empty", "NO_ASSERTION", 3),
        ("disabled", "UNCONDITIONAL_SKIP", 4),
        ("disabled", "ASSERT_TRUE", 5),
    }
    issues, count = analyze_file(path, root=tmp_path)
    assert count == report.tests_scanned
    assert set(issues) == set(report.issues)


@pytest.mark.parametrize("opener,closer", [("{", "}"), ("(", ")"), ("[", "]")])
def test_invocation_delimiters_and_nested_helpers(tmp_path: Path, opener: str, closer: str) -> None:
    path = _write(
        tmp_path / "test.rs",
        TEST_MACRO
        + f"""
cases!{opener}
    fn outer() {{
        fn helper() {{}}
        let text: &'static str = "fn string_helper() {{}}";
        let raw = r#"}} fn raw_helper() {{}}"#;
        /* nested /* }} fn comment_helper() {{}} */ comment */
        assert_eq!(actual(), expected());
    }}
    fn second() {{}} fn third() {{}}
{closer}
fn ordinary() {{}}
""",
    )
    report = audit_paths([path], root=tmp_path)
    assert report.tests_scanned == 3
    assert {(issue.test_name, issue.issue_code) for issue in report.issues} == {
        ("second", "NO_ASSERTION"),
        ("third", "NO_ASSERTION"),
    }


@pytest.mark.parametrize(
    "definition",
    [
        "macro_rules! cases { ($($item:item)*) => { $($item)* }; }",
        'macro_rules! cases { () => { let text = "#[test]"; }; }',
        'macro_rules! cases { () => { let text = r##"#[test]"##; }; }',
        "macro_rules! cases { () => { /* nested /* #[test] */ comment */ }; }",
        "// macro_rules! cases { () => { #[test] }; }",
        'const TEXT: &str = r#"macro_rules! cases { () => { #[test] }; }"#;',
    ],
)
def test_ordinary_macros_and_noncode_do_not_establish_tests(
    tmp_path: Path, definition: str
) -> None:
    path = _write(tmp_path / "test.rs", definition + "\ncases! { fn ordinary() {} }\n")
    report = audit_paths([path], root=tmp_path)
    assert report.tests_scanned == 0
    assert report.issues == ()


def test_templates_and_literal_invocations_are_not_test_instances(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test.rs",
        TEST_MACRO
        + """
macro_rules! unused { () => { #[test] fn template() {} }; }
// cases! { fn commented() {} }
/* cases! { fn commented_block() {} } */
const TEXT: &str = r###"
cases! { fn literal() {} }
#[test]
fn literal_plain() {}
"###;
cases! { fn actual() { assert_eq!(value(), expected()); } }
""",
    )
    report = audit_paths([path], root=tmp_path)
    assert report.tests_scanned == 1
    assert report.issues == ()


def test_macro_discovery_stays_inside_crate(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", '[workspace]\nmembers = ["first", "second"]\n')
    for name in ("first", "second", "first/nested"):
        _write(tmp_path / name / "Cargo.toml", f'[package]\nname = "{name}"\n')
    _write(tmp_path / "second/src/lib.rs", TEST_MACRO)
    _write(tmp_path / "first/nested/src/lib.rs", TEST_MACRO.replace("cases", "nested_cases"))
    _write(tmp_path / "first/target/generated.rs", TEST_MACRO.replace("cases", "generated_cases"))
    _write(tmp_path / "first/src/lib.rs", TEST_MACRO.replace("cases", "local_cases"))
    path = _write(
        tmp_path / "first/tests/test.rs",
        """
cases! { fn external() {} }
nested_cases! { fn nested_crate() {} }
generated_cases! { fn generated() {} }
other::local_cases! { fn qualified_external() {} }
local_cases! { fn local() {} }
""",
    )
    report = audit_paths([path], root=tmp_path)
    assert report.tests_scanned == 1
    assert [(issue.test_name, issue.issue_code) for issue in report.issues] == [
        ("local", "NO_ASSERTION")
    ]


def test_ambiguous_macro_names_do_not_infer_tests(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test.rs",
        TEST_MACRO
        + """
mod other {
    macro_rules! cases { ($($item:item)*) => { $($item)* }; }
    cases! { fn ordinary() {} }
}
""",
    )
    report = audit_paths([path], root=tmp_path)
    assert report.tests_scanned == 0
    assert report.issues == ()


def test_macro_definition_changes_are_visible_in_next_audit(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", '[package]\nname = "sample"\n')
    definition = _write(tmp_path / "src/lib.rs", TEST_MACRO)
    path = _write(tmp_path / "tests/test.rs", "cases! { fn emitted() {} }\n")
    assert audit_paths([path], root=tmp_path).tests_scanned == 1
    definition.write_text(TEST_MACRO.replace("#[test]", ""), encoding="utf-8")
    assert audit_paths([path], root=tmp_path).tests_scanned == 0


def test_parity_corpus_scans_its_full_inventory() -> None:
    root = Path(__file__).resolve().parents[2]
    report = audit_paths([root / "crates/gclient/tests/parity"], root=root)
    # Measured at this commit: 109 macro-emitted + 4 plain tests. Update this
    # inventory guard when parity cases are added or removed.
    assert report.tests_scanned == 113
    assert report.warnings == ()
