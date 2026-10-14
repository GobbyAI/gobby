"""Resolved Rust module scopes bind validation credit to the exact test."""

import pytest

from gobby.tasks.acceptance_artifacts import AcceptanceTest, rust_validation_run_names_test
from gobby.tasks.acceptance_rust_modules import resolve_rust_module


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
        ("crates/gcode/tests/root.rs", "", "root_test"),
        ("crates/gcode/src/lib.rs", "", "root_test"),
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
