//! Env-gated graph integration tests. CI can run this suite only when it
//! provisions a migrated PostgreSQL hub plus FalkorDB and exports
//! `GCODE_GRAPH_STANDALONE_DATABASE_URL`, `GCODE_GRAPH_STANDALONE_FALKOR_HOST`,
//! `GCODE_GRAPH_STANDALONE_FALKOR_PORT`, and optionally
//! `GCODE_GRAPH_STANDALONE_FALKOR_PASSWORD`.

mod common;

#[path = "graph_standalone/local_csharp_kotlin_ruby.rs"]
mod local_csharp_kotlin_ruby;
#[path = "graph_standalone/local_php_swift_dart_elixir.rs"]
mod local_php_swift_dart_elixir;
#[path = "graph_standalone/local_python_go_java.rs"]
mod local_python_go_java;
#[path = "graph_standalone/phantom_targets.rs"]
mod phantom_targets;
#[path = "graph_standalone/smoke.rs"]
mod smoke;
#[path = "graph_standalone/support.rs"]
mod support;
