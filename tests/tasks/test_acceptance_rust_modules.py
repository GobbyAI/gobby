"""Resolved Rust module scopes bind validation credit to the exact test."""

from datetime import UTC, datetime, timedelta

import pytest

from gobby.tasks.acceptance_artifacts import (
    AcceptanceTest,
    rust_validation_run_names_test,
    validation_run_covers_test,
    validation_run_names_test,
)
from gobby.tasks.acceptance_rust_modules import resolve_rust_module, resolve_rust_target
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_outcomes import EvidenceOutcome


@pytest.mark.parametrize("binary", [None, "root"])
@pytest.mark.parametrize("names_only", [True, False])
@pytest.mark.parametrize(
    "manifest,expected",
    [
        ("--manifest-path crates/example/Cargo.toml", True),
        ("--manifest-path=./crates/example/Cargo.toml", True),
        ("-p actual-package --manifest-path /abs/repo/crates/example/Cargo.toml", True),
        ("-p actual-package --manifest-path Cargo.toml", True),
        ("--manifest-path crates/other/Cargo.toml", False),
        ("--manifest-path Cargo.toml", False),
        ("--manifest-path crates/example/Cargo.toml -p other", False),
        ("--manifest-path crates/example/Cargo.toml --workspace", False),
    ],
)
def test_manifest_path_binds_root_acceptance_target(
    binary: str | None, names_only: bool, manifest: str, expected: bool
) -> None:
    path = "crates/example/tests/root.rs" if binary else "crates/example/src/lib.rs"
    test = AcceptanceTest(
        reference=f"{path}::root_test",
        path=path,
        symbol="root_test",
        body="fn root_test() { assert!(true); }",
        rust_module=(),
        rust_target=("actual-package", binary),
    )
    selector = f"--test {binary}" if binary else "--lib"
    command = f"cargo test {manifest} {selector} root_test"
    validate = validation_run_names_test if names_only else validation_run_covers_test

    assert validate(command, "test root_test ... ok", test) is expected


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_manifest_path_acceptance_requires_passing_run(outcome: EvidenceOutcome) -> None:
    started = datetime(2026, 10, 15, tzinfo=UTC)
    path = "crates/example/src/lib.rs"
    test = AcceptanceTest(
        reference=f"{path}::root_test",
        path=path,
        symbol="root_test",
        body="fn root_test() { assert!(feature()); }",
        rust_module=(),
        rust_target=("actual-package", None),
    )
    command = "cargo test --manifest-path crates/example/Cargo.toml --lib root_test"
    run_rows: tuple[tuple[int, str, EvidenceOutcome, str], ...] = (
        (
            2,
            "cargo test -p actual-package --lib root_test",
            "failure",
            "test root_test ... FAILED\nthread 'root_test' panicked at 'assertion failed'",
        ),
        (4, command, outcome, "test root_test ... ok"),
    )
    runs = tuple(
        TranscriptValidationRun(
            session_id="session",
            source="codex",
            command=run_command,
            categories=("test",),
            matcher_id="cargo-test",
            label="cargo test",
            outcome=run_outcome,
            started_at=started + timedelta(seconds=order),
            completed_at=started + timedelta(seconds=order + 1),
            order=order,
            exit_code=0 if run_outcome == "success" else 1,
            output=output,
        )
        for order, run_command, run_outcome, output in run_rows
    )
    evidence = TranscriptEvidence(
        edits=tuple(
            TranscriptEdit(
                session_id="session",
                source="codex",
                path=edit_path,
                timestamp=started + timedelta(seconds=order),
                order=order,
                tool_name="apply_patch",
            )
            for order, edit_path in ((1, path), (3, "crates/example/src/feature.rs"))
        ),
        validation_runs=runs,
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is (outcome == "success")
    assert result.green_runs == ((command,) if outcome == "success" else ())


@pytest.mark.parametrize(
    ("path", "module", "name"),
    [
        ("crates/example/src/config/tests.rs", ("config", "tests"), "config::tests::nested"),
        ("crates/example/tests/root.rs", ("tests",), "tests::nested"),
        ("crates/example/tests/root.rs", (), "nested"),
    ],
)
def test_nested_same_name_test_not_credited(path: str, module: tuple[str, ...], name: str) -> None:
    test = AcceptanceTest(
        reference=f"{path}::expected_symbol",
        path=path,
        symbol="expected_symbol",
        body="fn expected_symbol() { assert!(true); }",
        rust_module=module,
    )

    assert not rust_validation_run_names_test(
        f"PASS [0.004s] example::root {name}::expected_symbol", test
    )


@pytest.mark.parametrize(
    ("path", "module", "symbol"),
    [
        (
            "crates/gcode/src/commands/graph/view/render_tests.rs",
            "commands::graph::view::render::tests",
            "view_orders_size_ties_by_numeric_community_id",
        ),
        (
            "crates/gcode/src/commands/graph/view/communities/tests.rs",
            "commands::graph::view::communities::tests",
            "list_orders_communities_by_size_then_community_id",
        ),
        (
            "crates/gcode/src/commands/graph/view/mcg/tests.rs",
            "commands::graph::view::mcg::tests",
            "mcg_orders_size_ties_by_community_id",
        ),
    ],
)
@pytest.mark.parametrize(
    "output",
    [
        "test {name} ... ok",
        "PASS [0.004s] gobby-code {name}",
        '<testsuite><testcase name="{name}"/></testsuite>',
    ],
)
def test_exact_module_scope_still_credited(
    path: str, module: str, symbol: str, output: str
) -> None:
    test = AcceptanceTest(
        reference=f"{path}::{symbol}",
        path=path,
        symbol=symbol,
        body=f"fn {symbol}() {{ assert!(true); }}",
        rust_module=tuple(module.split("::")) if module else (),
    )

    name = f"{module}::{symbol}" if module else symbol
    assert rust_validation_run_names_test(output.format(name=name), test)


def test_unresolved_inline_fixture_still_credited() -> None:
    test = AcceptanceTest(
        reference="crates/example/src/config.rs::expected_symbol",
        path="crates/example/src/config.rs",
        symbol="expected_symbol",
        body="fn expected_symbol() { assert!(true); }",
    )

    assert rust_validation_run_names_test(
        "PASS [0.004s] example config::tests::expected_symbol", test
    )


@pytest.mark.asyncio
async def test_integration_crate_root_has_no_module_prefix() -> None:
    module = await resolve_rust_module(
        "crates/example/tests/parity.rs",
        "candidate",
        ".",
        cache={("candidate", "crates/example"): {}},
    )

    assert module == ()


@pytest.mark.parametrize("path", ["crates/example/tests/root.rs", "crates/example/src/lib.rs"])
@pytest.mark.parametrize(
    "output",
    [
        "PASS [0.004s] example::other root_test",
        "PASS [0.004s] other::root root_test",
        "test root_test ... ok",
        '<testcase name="root_test"/>',
    ],
)
def test_root_scope_rejects_unbound_target(path: str, output: str) -> None:
    test = AcceptanceTest(
        reference=f"{path}::root_test",
        path=path,
        symbol="root_test",
        body="fn root_test() { assert!(true); }",
        rust_module=(),
        rust_target=("example", "root") if "/tests/" in path else ("example", None),
    )

    assert not rust_validation_run_names_test(output, test)
    assert not validation_run_names_test("cargo nextest run --workspace", output, test)
    assert not validation_run_covers_test("cargo nextest run --workspace", output, test)


@pytest.mark.parametrize("module", [(), ("helpers",)])
@pytest.mark.parametrize("binary", ["root", None])
@pytest.mark.parametrize("format", ["nextest", "cargo"])
def test_root_scope_requires_matching_target(
    binary: str | None, format: str, module: tuple[str, ...]
) -> None:
    path = "crates/example/tests/root.rs" if binary else "crates/example/src/lib.rs"
    test = AcceptanceTest(
        reference=f"{path}::root_test",
        path=path,
        symbol="root_test",
        body="fn root_test() { assert!(true); }",
        rust_module=module,
        rust_target=("actual-package", binary),
    )
    target = f"actual-package::{binary}" if binary else "actual-package"
    selector = f"--test {binary}" if binary else "--lib"
    name = "::".join((*module, "root_test"))
    output = f"PASS [0.004s] {target} {name}" if format == "nextest" else f"test {name} ... ok"
    command = f"cargo test -p actual-package {selector} {name}"
    assert validation_run_names_test(command, output, test)
    assert validation_run_covers_test(command, output, test)


@pytest.mark.parametrize("module", [(), ("helpers",)])
@pytest.mark.parametrize(
    "command",
    [
        "cargo test -p example --test other root_test",
        "cargo test -p other --test root root_test",
    ],
)
def test_root_command_rejects_other_target(module: tuple[str, ...], command: str) -> None:
    path = "crates/example/tests/root.rs"
    test = AcceptanceTest(
        reference=f"{path}::root_test",
        path=path,
        symbol="root_test",
        body="fn root_test() { assert!(true); }",
        rust_module=module,
        rust_target=("example", "root"),
    )
    output = "test " + "::".join((*module, "root_test")) + " ... ok"

    assert not validation_run_names_test(command, output, test)
    assert not validation_run_covers_test(command, output, test)


def test_exact_name_is_not_prefix_of_a_deeper_path() -> None:
    test = AcceptanceTest(
        reference="crates/example/src/a/tests.rs::test_x",
        path="crates/example/src/a/tests.rs",
        symbol="test_x",
        body="fn test_x() { assert!(true); }",
        rust_module=("a", "tests"),
    )

    assert not rust_validation_run_names_test("test a::tests::test_x::inner ... ok", test)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "manifest", "target"),
    [
        ("tests/root.rs", '[package]\nname="actual-package"', ("actual-package", "root")),
        ("src/lib.rs", '[package]\nname="actual-package"', ("actual-package", None)),
        (
            "tests/root.rs",
            '[package]\nname="actual-package"\n[[test]]\nname="renamed"\npath="tests/root.rs"',
            ("actual-package", "renamed"),
        ),
        (
            "tests/root.rs",
            '[package]\nname="actual-package"\n[[test]]\nname="root"\npath="tests/other.rs"',
            None,
        ),
        (
            "tests/root.rs",
            '[package]\nname="actual-package"\nautotests=false\n[[test]]\nname="root"',
            ("actual-package", "root"),
        ),
    ],
)
async def test_root_target_comes_from_committed_manifest(
    path: str, manifest: str, target: tuple[str, str | None] | None
) -> None:
    actual = await resolve_rust_target(
        f"crates/directory/{path}",
        "candidate",
        ".",
        cache={"crates/directory/Cargo.toml": manifest},
    )

    assert actual == target


@pytest.mark.parametrize("binary,module", [("root", ()), ("root", ("helpers",)), (None, ())])
@pytest.mark.parametrize("names_only", [True, False])
@pytest.mark.parametrize(
    "command_template,output_template,expected",
    [
        ("{base} other_test -- --skip {name}", "test other_test ... ok", False),
        ("{base} -- --skip {name}", "test {name} ... ok", False),
        ("{base} -- --skip={name}", "test {name} ... ok", False),
        ("{base} -- --skip {name}", "PASS [0.004s] {label} {name}", False),
        ("{base} -- --logfile {name}", "test other_test ... ok", False),
        ("{base} -- --logfile {path}::{name}", "test other_test ... ok", False),
        ("{base} {name}", "", False),
        ("{base} {name}", "test sibling ... ok", False),
        ("cargo test -- -p example {selector} {name}", "test {name} ... ok", False),
        ("cargo check -p example {selector} {name}", "test {name} ... ok", False),
        ("{base} --target {name}", "test other_test ... ok", False),
        ("{base} --features {name}", "test other_test ... ok", False),
        ("{base} --features={name}", "test other_test ... ok", False),
        ("{base} {name}", "Running {path}", False),
        ("{base} {name}", "Running tests/root.rs", False),
        ("{base} {name}", "test {name}_long ... ok\nRunning unittests {path}", False),
        ("{base} {name} -- --skip", "test {name} ... ok", False),
        ("{base} root -- --exact", "test {name} ... ok", False),
        ("{base} {name}", "test {name}::inner ... ok", False),
        ("{base} {name}", "SKIP [0.004s] {label} {name}", False),
        ("{base} {name}", "PASS [0.004s] {label} {name}::inner", False),
        ("cargo test --workspace", "test {name} ... ok", False),
        ("{base} --package --test {name}", "test {name} ... ok", False),
        ("{base} {name} sibling", "test {name} ... ok", False),
        ("{base} -p example {name}", "test {name} ... ok", False),
        ("{base} {name}", "test {name} ... ok", True),
        ("{base}", "test {name} ... FAILED", True),
        ("{base} root", "test {name} ... ok", True),
        ("{base} {name} -- --exact", "test {name} ... ok", True),
        ("cargo test --package=example {selector_equals} {name}", "test {name} ... ok", True),
        ("{base} {name}", "PASS [0.004s] {label} {name}", True),
        ("{base} {name}", "FAIL [0.004s] {label} {name}", True),
        ("cargo nextest run -p example", "PASS [0.004s] {label} {name}", True),
    ],
)
def test_root_credit_requires_executed_row(
    binary: str | None,
    module: tuple[str, ...],
    names_only: bool,
    command_template: str,
    output_template: str,
    expected: bool,
) -> None:
    path = "crates/example/tests/root.rs" if binary else "crates/example/src/lib.rs"
    name = "::".join((*module, "root_test"))
    selector = f"--test {binary}" if binary else "--lib"
    test = AcceptanceTest(
        reference=f"{path}::root_test",
        path=path,
        symbol="root_test",
        body="fn root_test() { assert!(true); }",
        rust_module=module,
        rust_target=("example", binary),
    )
    values = {
        "base": f"cargo test -p example {selector}",
        "name": name,
        "selector": selector,
        "selector_equals": f"--test={binary}" if binary else "--lib",
        "label": f"example::{binary}" if binary else "example",
        "path": path,
    }
    command = command_template.format(**values)
    output = output_template.format(**values)
    validate = validation_run_names_test if names_only else validation_run_covers_test

    assert validate(command, output, test) is expected


@pytest.mark.parametrize("module", [("a", "tests"), None])
@pytest.mark.parametrize("names_only", [True, False])
@pytest.mark.parametrize(
    "command,output,expected",
    [
        ("cargo test -p example -- --skip a::tests::test_x", "test sibling ... ok", False),
        ("cargo test -p example -- --skip=a::tests::test_x", "test a::tests::test_x ... ok", False),
        ("JOBS=2 cargo test -- --skip a::tests::test_x", "test a::tests::test_x ... ok", False),
        ("cargo test -p example --skip a::tests::test_x", "test a::tests::test_x ... ok", False),
        ("cargo test -p example --features a::tests::test_x", "test sibling ... ok", False),
        ("cargo test -p example a::tests::test_x", "", False),
        ("cargo test -p example a::tests::test_x", "test sibling ... ok", False),
        ("cargo test -p example a::tests::test_x", "Running crates/example/src/a.rs", False),
        ("cargo check -p example a::tests::test_x", "test a::tests::test_x ... ok", False),
        ("cargo test -- -p example", "test a::tests::test_x ... ok", False),
        ("cargo test a::tests::test_x -- --skip", "test a::tests::test_x ... ok", False),
        ("cargo test other", "test a::tests::test_x ... ok", False),
        ("cargo test a::tests::test_x", "echo a::tests::test_x", False),
        ("cargo test", "SKIP [0.004s] example a::tests::test_x", False),
        ("cargo test", "test a::tests::test_x::inner ... ok", False),
        ("cargo test a::tests::test_x", "test a::tests::test_x ... ok", True),
        ("cargo test", "test a::tests::test_x ... FAILED", True),
        ("cargo test", "PASS [0.004s] example a::tests::test_x", True),
        ("cargo test", "FAIL [0.004s] example a::tests::test_x", True),
        ("cargo test", "PASS example a::tests::test_x", True),
        ("cargo nextest run", "FAIL example a::tests::test_x", True),
        ("cargo test", '<testsuite><testcase name="a::tests::test_x"/></testsuite>', True),
    ],
)
def test_non_root_credit_uses_executed_output(
    module: tuple[str, ...] | None, names_only: bool, command: str, output: str, expected: bool
) -> None:
    test = AcceptanceTest(
        reference="crates/example/src/a.rs::test_x",
        path="crates/example/src/a.rs",
        symbol="test_x",
        body="fn test_x() { assert!(true); }",
        rust_module=module,
        rust_target=("example", None),
    )
    validate = validation_run_names_test if names_only else validation_run_covers_test
    assert validate(command, output, test) is expected


@pytest.mark.parametrize("names_only", [True, False])
@pytest.mark.parametrize("diagnostic", ["tests/foo.rs", "crates/example/tests/foo.rs"])
def test_rust_generic_fallback_cannot_credit_diagnostic(names_only: bool, diagnostic: str) -> None:
    test = AcceptanceTest(
        reference="crates/example/tests/foo.rs::test_x",
        path="crates/example/tests/foo.rs",
        symbol="test_x",
        body="fn test_x() { assert!(true); }",
        rust_module=(),
        rust_target=("example", "foo"),
    )
    validate = validation_run_names_test if names_only else validation_run_covers_test
    assert not validate("cargo test -p other --test foo test_x", f"Running {diagnostic}", test)


@pytest.mark.parametrize("names_only", [True, False])
@pytest.mark.parametrize(
    "command,output,expected",
    [
        ("cargo test", 'echo <testcase name="a::tests::test_x"/>', False),
        ("cargo test", 'log: <testsuite><testcase name="a::tests::test_x"/></testsuite>', False),
        ("cargo test", '<testsuite><testcase name="a::tests::test_x"/></testsuite> log', False),
        ("cargo test", '<testcase name="a::tests::test_x"/>', False),
        ("cargo test", '<testsuites><testcase name="a::tests::test_x"/></testsuites>', False),
        ("cargo test", '<testsuite><testcase name="a::tests::test_x"/>', False),
        ("cargo test", '<testsuite><testcase name="a::tests::other"/></testsuite>', False),
        ("cargo test", '<testsuite><testcase name="b::tests::test_x"/></testsuite>', False),
        (
            "cargo test",
            '<testsuite><testcase classname="a::tests"/><testcase name="test_x"/></testsuite>',
            False,
        ),
        ("cargo test", '<testsuite><testcase classname="a::tests::test_x"/></testsuite>', False),
        (
            "cargo test",
            '<!DOCTYPE testsuite><testsuite><testcase name="a::tests::test_x"/></testsuite>',
            False,
        ),
        (
            "cargo test",
            '<!DOCTYPE testsuite [<!ENTITY test "a::tests::test_x">]>'
            '<testsuite><testcase name="&test;"/></testsuite>',
            False,
        ),
        ("cargo test", '<testsuite><testcase name="a::tests::test_x"/></testsuite>', True),
        (
            "cargo test",
            '<testsuites><testsuite><testcase name="a::tests::test_x"/></testsuite></testsuites>',
            True,
        ),
        (
            "cargo test",
            '<testsuite><testcase classname="a::tests" name="test_x"/></testsuite>',
            True,
        ),
        (
            "cargo test -- --skip a::tests::test_x",
            '<testsuite><testcase name="a::tests::test_x"/></testsuite>',
            False,
        ),
    ],
)
def test_junit_credit_requires_complete_suite(
    names_only: bool, command: str, output: str, expected: bool
) -> None:
    test = AcceptanceTest(
        reference="crates/example/src/a.rs::test_x",
        path="crates/example/src/a.rs",
        symbol="test_x",
        body="fn test_x() { assert!(true); }",
        rust_module=("a", "tests"),
    )
    validate = validation_run_names_test if names_only else validation_run_covers_test
    assert validate(command, output, test) is expected


@pytest.mark.parametrize("names_only", [True, False])
@pytest.mark.parametrize("module", [(), ("a", "tests")])
@pytest.mark.parametrize("result", ["PASS", "FAIL"])
@pytest.mark.parametrize("counter", ["(1/1)", "(  3/120)"])
def test_nextest_progress_counter_keeps_executed_credit(
    names_only: bool, module: tuple[str, ...], result: str, counter: str
) -> None:
    path = "crates/example/src/a.rs" if module else "crates/example/src/lib.rs"
    name = "::".join((*module, "test_x"))
    test = AcceptanceTest(
        reference=f"{path}::test_x",
        path=path,
        symbol="test_x",
        body="fn test_x() { assert!(true); }",
        rust_module=module,
        rust_target=("example", None),
    )
    validate = validation_run_names_test if names_only else validation_run_covers_test
    assert validate("cargo nextest run", f"{result} [ 0.001s] {counter} example {name}", test)


@pytest.mark.parametrize("names_only", [True, False])
@pytest.mark.parametrize(
    "flags,expected",
    [
        ("--workspace", True),
        ("--all-targets", True),
        ("--tests", True),
        ("--lib", True),
        ("-p other -p example", True),
        ("--package other --package example", True),
        ("--unknown-selection", False),
    ],
)
def test_broadening_flags_keep_per_test_credit(
    names_only: bool, flags: str, expected: bool
) -> None:
    path = "crates/example/src/a.rs"
    test = AcceptanceTest(
        reference=f"{path}::test_x",
        path=path,
        symbol="test_x",
        body="fn test_x() { assert!(true); }",
        rust_module=("a", "tests"),
        rust_target=("example", None),
    )
    validate = validation_run_names_test if names_only else validation_run_covers_test
    command = f"cargo test -p example {flags}"
    assert validate(command, "test a::tests::test_x ... ok", test) is expected
    assert not validate(command, "test sibling ... ok", test)


@pytest.mark.parametrize("names_only", [True, False])
@pytest.mark.parametrize("binary", [None, "foo"])
@pytest.mark.parametrize(
    "selectors",
    [
        "-p example -p other {selector} test_x",
        "-p example {selector} --tests test_x",
        "-p example {selector} --all-targets test_x",
        "-p example {selector} --workspace test_x",
        "-p example --lib --test foo",
    ],
)
def test_root_binding_rejects_broadening_selectors(
    names_only: bool, binary: str | None, selectors: str
) -> None:
    path = "crates/example/tests/foo.rs" if binary else "crates/example/src/lib.rs"
    test = AcceptanceTest(
        reference=f"{path}::test_x",
        path=path,
        symbol="test_x",
        body="fn test_x() { assert!(true); }",
        rust_module=(),
        rust_target=("example", binary),
    )
    command = "cargo test " + selectors.format(selector="--test foo" if binary else "--lib")
    validate = validation_run_names_test if names_only else validation_run_covers_test
    assert not validate(command, "test test_x ... ok", test)


@pytest.mark.parametrize("names_only", [True, False])
@pytest.mark.parametrize("outcome", ["skipped", "failure", "error"])
def test_junit_non_success_outcome_is_not_credited(names_only: bool, outcome: str) -> None:
    test = AcceptanceTest(
        reference="crates/example/src/a.rs::test_x",
        path="crates/example/src/a.rs",
        symbol="test_x",
        body="fn test_x() { assert!(true); }",
        rust_module=("a", "tests"),
    )
    output = f'<testsuite><testcase name="a::tests::test_x"><{outcome}/></testcase></testsuite>'
    validate = validation_run_names_test if names_only else validation_run_covers_test
    assert not validate("cargo test", output, test)
