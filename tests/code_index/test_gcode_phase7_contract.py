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
            ROOT,
            ROOT.parent / "gobby-cli",
            Path.home() / "Projects" / "gobby-cli",
        ]
    )

    for candidate in candidates:
        if (candidate / "crates/gcode/Cargo.toml").is_file():
            return candidate

    pytest.skip(
        "Phase 7 contract tests require the Rust workspace. Set GOBBY_CLI_REPO to a fallback repo root."
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
    return re.sub(
        r"#\[cfg\(test\)\]\s*mod\s+tests\b.*",
        "",
        source,
        count=1,
        flags=re.S,
    )


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

    assert "grant::acquire" in config
    assert "db::falkor_from_grant" in config
    assert "fn falkor_from_grant" in config
    graph_name_literal = re.search(r"graph_name:\s*\"gobby_code\"\.to_string\(\)", config)
    graph_name_const = 'const FALKORDB_GRAPH_NAME: &str = "gobby_code";' in config and re.search(
        r"graph_name:\s*FALKORDB_GRAPH_NAME\.to_string\(\)", config
    )
    graph_name_core_const = (
        "pub const FALKORDB_GRAPH_NAME: &str = gobby_core::config::CODE_GRAPH_NAME;" in config
        and re.search(r"graph_name:\s*FALKORDB_GRAPH_NAME\.to_string\(\)", config)
    )
    assert graph_name_literal or graph_name_const or graph_name_core_const
    _assert_contains_all(
        config,
        (
            "databases.falkordb.host",
            "databases.falkordb.port",
            "databases.falkordb.password",
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
    config = "\n".join(
        (
            _read("crates/gcode/src/config/context.rs"),
            _read("crates/gcode/src/config/services.rs"),
            _read("crates/gcode/src/db/resolution.rs"),
        )
    )
    config_facade = _read("crates/gcode/src/config.rs")
    context = _struct_body(config, "Context")

    _assert_falkordb_config(config, context)
    _assert_neo4j_transition_state(config, context)
    _assert_contains_all(
        config_facade,
        (
            "mod context;",
            "pub use context::{",
            "Context",
            "FALKORDB_GRAPH_NAME",
            "FalkorConfig",
        ),
    )


def test_phase7_falkor_client_pins_core_graph_client_facade_contract() -> None:
    """The Falkor facade delegates connection details to gobby-core."""
    falkor = _read("crates/gcore/src/falkor.rs")
    connection = _read("crates/gcode/src/graph/code_graph/connection.rs")

    client = _struct_body(falkor, "GraphClient")
    _assert_fields(client, ("connection: Connection", "graph_name: String"))

    _assert_contains_all(
        falkor,
        (
            "pub type Row = HashMap<String, Value>",
            "pub struct GraphClient",
            "GraphClient::from_config",
            "with_graph_client",
            "DEFAULT_CONNECT_TIMEOUT",
            "DEFAULT_SOCKET_TIMEOUT",
            "set_read_timeout",
            "set_write_timeout",
            "GRAPH.QUERY",
        ),
    )
    _assert_matches(
        falkor,
        (
            r"pub\s+type\s+Row\s*=\s*HashMap<String,\s*Value>",
            r"pub\s+fn\s+from_config\(config:\s*&FalkorConfig,\s*graph_name:\s*&str\)",
            r"pub\s+fn\s+from_config_with_timeouts\(",
            r"pub\s+fn\s+query\(\s*&mut\s+self,\s*cypher:\s*&str,\s*"
            r"params:\s*Option<HashMap<String,\s*String>>",
            r"pub\s+fn\s+with_graph<T>\(\s*config:\s*Option<&FalkorConfig>,\s*"
            r"graph_name:\s*&str,\s*default:\s*T,\s*"
            r"f:\s*impl\s+FnOnce\(&mut\s+GraphClient\)",
        ),
    )
    _assert_contains_all(
        connection,
        (
            "use gobby_core::falkor::GraphClient",
            "config.connection_config()",
            "gobby_core::falkor::with_graph",
            "ctx.falkordb",
            "&config.graph_name",
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
    assert "reqwest" in dependencies
    gcore = _toml("crates/gcore/Cargo.toml")
    assert "base64" in gcore["dependencies"]

    package_names = {package["name"] for package in lockfile["package"]}
    assert "gobby-core" in package_names
    assert "redis" in package_names
    assert "falkordb" not in package_names
    assert "urlencoding" in package_names
    assert "neo4j" not in package_names
    assert "neo4rs" not in package_names


_REMOVED_CREDENTIAL_MARKERS: tuple[str, ...] = (
    "GCODE_DATABASE_URL",
    "GWIKI_DATABASE_URL",
    "GOBBY_POSTGRES_DSN",
    "GOBBY_FALKORDB_",
    "GOBBY_QDRANT_",
)
_CRATE_AUDIT_ROOTS: tuple[str, ...] = ("gcode", "gcore", "gwiki", "gdaemon", "ghook")
_GRANT_CAPABILITY_MARKERS: tuple[str, ...] = (
    "PostgresCapability",
    "FalkorCapability",
    "QdrantCapability",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
    "write_managed_bootstrap",
)


def _iter_crate_sources() -> list[Path]:
    repo = _gobby_cli_repo()
    files: list[Path] = []
    for crate in _CRATE_AUDIT_ROOTS:
        root = repo / "crates" / crate
        if not root.is_dir():
            continue
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in {".rs", ".toml", ".json", ".md"}
            and path.name != "CHANGELOG.md"
            and "tests" not in path.parts
        )
    return files


def test_contract_on_grant_fixtures() -> None:
    """Phase-7 and crate fixtures bind services through signed grants only."""
    repo = _gobby_cli_repo()
    context = _read("crates/gcode/src/config/context.rs")
    resolution = _read("crates/gcode/src/db/resolution.rs")
    gwiki_common = _read("crates/gwiki/tests/common/mod.rs")
    storage_conformance = (repo / "tests/code_index/test_gcode_storage_conformance.py").read_text()

    for fragment in _GRANT_CAPABILITY_MARKERS:
        assert fragment in resolution or fragment in context or fragment in gwiki_common, fragment

    assert "postgres_dsn_from_grant" in resolution
    assert "falkor_from_grant" in resolution
    assert "qdrant_from_grant" in resolution
    assert "GOBBY_MANAGED_EXECUTION_BOOTSTRAP" in gwiki_common
    assert "GOBBY_RUNTIME_MODE" not in storage_conformance
    for marker in (
        "StandaloneSetup",
        "GOBBY_RUNTIME_MODE=standalone",
        "runtime_mode = standalone",
    ):
        assert marker not in storage_conformance
    for marker in ("GCODE_DATABASE_URL", "GOBBY_POSTGRES_DSN", "gcore.yaml"):
        assert marker not in storage_conformance

    leaked: list[str] = []
    for path in _iter_crate_sources():
        text = path.read_text(errors="replace")
        if "gcore.yaml" in text and path.suffix in {".rs", ".toml"}:
            leaked.append(f"{path.relative_to(repo)}:gcore.yaml")
        for marker in _REMOVED_CREDENTIAL_MARKERS:
            if marker in text:
                leaked.append(f"{path.relative_to(repo)}:{marker}")
    assert leaked == []


def test_phase7_ports_all_eight_read_queries_without_unbound_numeric_params() -> None:
    """Phase 7.3 ports every Rust graph read helper to FalkorDB query semantics."""
    code_graph_facade = _read("crates/gcode/src/graph/code_graph.rs")
    graph_relationships = _without_rust_unit_tests(
        _read("crates/gcode/src/graph/code_graph/read/relationships.rs")
    )
    graph_relationship_queries = _without_rust_unit_tests(
        _read("crates/gcode/src/graph/code_graph/read/relationship_queries.rs")
    )
    graph_read_support = _without_rust_unit_tests(
        _read("crates/gcode/src/graph/code_graph/read/support.rs")
    )
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
            graph_relationships,
        ), f"missing public read helper {function}(ctx: &Context, ...)"
        assert function in code_graph_facade

    _assert_contains_all(
        code_graph_facade,
        (
            "mod read;",
            "pub use read::{",
            "pub(crate) use read::{",
            "get_imports_query",
        ),
    )
    _assert_contains_all(
        graph_relationship_queries,
        (
            "depth.clamp(1, 5)",
            "SKIP {offset} LIMIT {limit}",
            "target.id IN [{ids}]",
            "src.id IN [{ids}]",
            "LIMIT {limit}",
        ),
    )
    _assert_contains_all(
        graph_read_support,
        (
            "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol",
            "typed_query::clamp_limit(limit, MAX_GRAPH_LIMIT)",
            "fn clamp_offset(offset: usize)",
        ),
    )
    _assert_matches(
        typed_query,
        (
            r"pub\s+fn\s+cypher_string_literal\(s:\s*&str\)\s*->\s*String",
            r"pub\s+fn\s+id_list_literal\(ids:\s*&\[String\]\)\s*->\s*String",
            r"pub\s+fn\s+clamp_limit\(limit:\s*usize,\s*max:\s*usize\)\s*->\s*usize",
        ),
    )
    assert re.search(
        r"fn\s+blast_radius_query\(depth:\s*usize,\s*limit:\s*usize\)",
        graph_relationship_queries,
    )
    assert "$offset" not in graph_relationship_queries
    assert "$limit" not in graph_relationship_queries
    assert "$ids" not in graph_relationship_queries
