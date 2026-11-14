//! Integration suite for `gobby-daemon`.
//!
//! Each top-level test file is one module of this program, so the crate builds a
//! single integration test binary. A module that needs a feature carries its `cfg`
//! here, where its former `required-features` gated a separate binary.

// Shared helpers load once here; every test module reaches them through `super`.
#[path = "common/mod.rs"]
mod common;

#[path = "fixtures/host_process.rs"]
mod host_process_fixture;

#[path = "node_support/mod.rs"]
mod node_support;

#[path = "cli_contract.rs"]
mod cli_contract;

#[path = "compare.rs"]
mod compare;

#[path = "front_door.rs"]
mod front_door;

#[path = "heartbeat.rs"]
mod heartbeat;

#[path = "http_contracts.rs"]
mod http_contracts;

#[path = "lease.rs"]
mod lease;

#[path = "lifecycle.rs"]
mod lifecycle;

#[path = "native_ws_transport.rs"]
mod native_ws_transport;

#[path = "node_agents.rs"]
mod node_agents;

#[path = "node_command_corpus.rs"]
mod node_command_corpus;

#[path = "node_command_parity.rs"]
mod node_command_parity;

#[path = "node_hooks.rs"]
mod node_hooks;

#[path = "node_internal_route.rs"]
mod node_internal_route;

#[path = "nodes.rs"]
mod nodes;

#[path = "pane_detection_corpus.rs"]
mod pane_detection_corpus;

#[path = "pane_detection_warnings.rs"]
mod pane_detection_warnings;

#[path = "pid_file_golden.rs"]
mod pid_file_golden;

#[path = "release_workflow.rs"]
mod release_workflow;

#[path = "run_modes.rs"]
mod run_modes;

#[path = "schema_cli.rs"]
mod schema_cli;

#[path = "ws_broker.rs"]
mod ws_broker;

#[path = "ws_envelope_golden.rs"]
mod ws_envelope_golden;

#[path = "ws_golden_proxy.rs"]
mod ws_golden_proxy;
