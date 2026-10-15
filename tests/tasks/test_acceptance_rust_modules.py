"""Resolved Rust module scopes bind validation credit to the exact test."""

import pytest

from gobby.tasks.acceptance_artifacts import (
    AcceptanceTest,
    rust_validation_run_names_test,
    validation_run_covers_test,
    validation_run_names_test,
)
from gobby.tasks.acceptance_rust_modules import resolve_rust_module, resolve_rust_target


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
    ["test {name} ... ok", "PASS [0.004s] gobby-code {name}", '<testcase name="{name}"/>'],
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
    output = (
        f"PASS [0.004s] {target} {name}"
        if format == "nextest"
        else f"cargo test -p actual-package {selector} {name}"
    )

    assert rust_validation_run_names_test(output, test)


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
