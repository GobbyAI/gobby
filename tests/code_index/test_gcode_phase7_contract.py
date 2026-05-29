"""Phase 7 gobby-code FalkorDB migration contract tests."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _gobby_cli_repo() -> Path:
    candidates: list[Path] = []
    if env_path := os.environ.get("GOBBY_CLI_REPO"):
        candidates.append(Path(env_path))
    candidates.extend(
        [
            ROOT.parent / "gobby-cli",
            Path.home() / "Projects" / "gobby-cli",
        ]
    )

    for candidate in candidates:
        if (candidate / "crates/gcode/Cargo.toml").is_file():
            return candidate

    pytest.skip(
        "Phase 7 contract tests require a gobby-cli checkout. Set GOBBY_CLI_REPO to the repo root."
    )


def _read(relative_path: str) -> str:
    return (_gobby_cli_repo() / relative_path).read_text()


def _toml(relative_path: str) -> dict[str, Any]:
    return tomllib.loads(_read(relative_path))


def _struct_body(source: str, name: str) -> str:
    match = re.search(rf"pub\s+struct\s+{name}\s*\{{(?P<body>.*?)\n\}}", source, re.S)
    assert match is not None, f"missing public Rust struct {name}"
    return match.group("body")


def _without_rust_unit_tests(source: str) -> str:
    return source.split("#[cfg(test)]", maxsplit=1)[0]


def _assert_field(body: str, declaration: str) -> None:
    assert declaration in body, f"missing field declaration: {declaration}"


def _assert_fields(body: str, declarations: tuple[str, ...]) -> None:
    for declaration in declarations:
        _assert_field(body, declaration)


def _assert_contains_all(source: str, fragments: tuple[str, ...]) -> None:
    for fragment in fragments:
        assert fragment in source


def _assert_matches(source: str, patterns: tuple[str, ...]) -> None:
    for pattern in patterns:
        assert re.search(pattern, source, re.S), f"missing pattern: {pattern}"


def _assert_falkordb_config(config: str, context: str) -> None:
    falkor = _struct_body(config, "FalkorConfig")
    _assert_fields(
        falkor,
        (
            "pub host: String",
            "pub port: u16",
            "pub password: Option<String>",
            "pub graph_name: String",
        ),
    )
    _assert_field(context, "pub falkordb: Option<FalkorConfig>")

    assert re.search(r"let\s+falkordb\s*=\s*resolve_falkordb_config\(", config)
    graph_name_literal = re.search(r"graph_name:\s*\"gobby_code\"\.to_string\(\)", config)
    graph_name_const = 'const FALKORDB_GRAPH_NAME: &str = "gobby_code";' in config and re.search(
        r"graph_name:\s*FALKORDB_GRAPH_NAME\.to_string\(\)", config
    )
    assert graph_name_literal or graph_name_const
    _assert_contains_all(
        config,
        (
            "databases.falkordb.host",
            "databases.falkordb.port",
            "databases.falkordb.requirepass",
            "GOBBY_FALKORDB_HOST",
            "GOBBY_FALKORDB_PORT",
            "GOBBY_FALKORDB_PASSWORD",
        ),
    )


def _assert_neo4j_transition_state(config: str, context: str) -> None:
    if re.search(r"pub\s+struct\s+Neo4jConfig\s*\{", config):
        neo4j = _struct_body(config, "Neo4jConfig")
        _assert_fields(
            neo4j,
            (
                "pub url: String",
                "pub auth: Option<String>",
                "pub database: String",
            ),
        )
        _assert_field(context, "pub neo4j: Option<Neo4jConfig>")
        assert re.search(r"let\s+neo4j\s*=\s*resolve_neo4j_config\(", config)
    else:
        assert "pub neo4j: Option<Neo4jConfig>" not in context
        assert "resolve_neo4j_config" not in config


def test_phase7_config_tracks_falkordb_and_neo4j_cutover() -> None:
    """FalkorDB config is required; Neo4j config is transitional until 7.4."""
    config = _read("crates/gcode/src/config.rs")
    context = _struct_body(config, "Context")

    _assert_falkordb_config(config, context)
    _assert_neo4j_transition_state(config, context)


def test_phase7_falkor_client_pins_core_graph_client_facade_contract() -> None:
    """The Falkor facade delegates connection details to gobby-core."""
    falkor = _read("crates/gcode/src/falkor.rs")

    client = _struct_body(falkor, "FalkorClient")
    _assert_field(client, "client: GraphClient")

    _assert_contains_all(
        falkor,
        (
            "use gobby_core::falkor::GraphClient",
            "pub type Row = gobby_core::falkor::Row",
            "GraphClient::from_config",
            "config.connection_config()",
            "with_core_client",
            "let mut client =",
            "ctx.falkordb",
        ),
    )
    _assert_matches(
        falkor,
        (
            r"pub\s+type\s+Row\s*=\s*gobby_core::falkor::Row",
            r"pub\s+fn\s+from_config\(config:\s*&FalkorConfig\)",
            r"pub\s+fn\s+query\(\s*&mut\s+self,\s*cypher:\s*&str,\s*"
            r"params:\s*Option<HashMap<String,\s*String>>",
            r"pub\s+fn\s+with_falkor<T>\(\s*ctx:\s*&Context,\s*default:\s*T,\s*"
            r"f:\s*impl\s+FnOnce\(&mut\s+FalkorClient\)",
        ),
    )


def test_phase7_cargo_dependencies_and_lockfile_track_falkordb_client() -> None:
    """FalkorDB deps are supplied through gobby-core while the lockfile stays pinned."""
    cargo = _toml("crates/gcode/Cargo.toml")
    lockfile = _toml("Cargo.lock")

    assert cargo["package"]["name"] == "gobby-code"
    assert {"name": "gcode", "path": "src/main.rs"} in cargo["bin"]

    dependencies = cargo["dependencies"]
    assert "falkor" in dependencies["gobby-core"]["features"]
    assert dependencies["urlencoding"] == "2"
    assert "base64" in dependencies
    assert "reqwest" in dependencies

    package_names = {package["name"] for package in lockfile["package"]}
    assert "gobby-core" in package_names
    assert "falkordb" in package_names
    assert "urlencoding" in package_names
    assert "neo4j" not in package_names
    assert "neo4rs" not in package_names


def test_phase7_ports_all_eight_read_queries_without_unbound_numeric_params() -> None:
    """Phase 7.3 ports every Rust graph read helper to FalkorDB query semantics."""
    falkor = _read("crates/gcode/src/falkor.rs")
    production = _without_rust_unit_tests(falkor)
    code_graph = _without_rust_unit_tests(_read("crates/gcode/src/graph/code_graph.rs"))
    typed_query = _without_rust_unit_tests(_read("crates/gcode/src/graph/typed_query.rs"))

    read_helpers = (
        "count_callers",
        "count_usages",
        "find_callers",
        "find_usages",
        "find_callers_batch",
        "find_callees_batch",
        "get_imports",
        "blast_radius",
    )
    for function in read_helpers:
        assert re.search(
            rf"pub\s+fn\s+{function}\(\s*ctx:\s*&Context\b",
            production,
        ), f"missing public read helper {function}(ctx: &Context, ...)"
        assert f"crate::graph::code_graph::{function}" in production

    _assert_contains_all(
        code_graph,
        (
            "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol",
            "depth.clamp(1, 5)",
            "typed_query::clamp_limit(limit, MAX_GRAPH_LIMIT)",
            "typed_query::clamp_offset(offset, MAX_GRAPH_LIMIT)",
            "SKIP {offset} LIMIT {limit}",
            "target.id IN [{ids}]",
            "src.id IN [{ids}]",
            "LIMIT {limit}",
        ),
    )
    _assert_matches(
        typed_query,
        (
            r"pub\s+fn\s+cypher_string_literal\(s:\s*&str\)\s*->\s*String",
            r"pub\s+fn\s+id_list_literal\(ids:\s*&\[String\]\)\s*->\s*String",
            r"pub\s+fn\s+clamp_offset\(offset:\s*usize,\s*max:\s*usize\)\s*->\s*usize",
            r"pub\s+fn\s+clamp_limit\(limit:\s*usize,\s*max:\s*usize\)\s*->\s*usize",
        ),
    )
    assert re.search(r"fn\s+blast_radius_query\(depth:\s*usize,\s*limit:\s*usize\)", code_graph)
    assert "$offset" not in code_graph
    assert "$limit" not in code_graph
    assert "$ids" not in code_graph
