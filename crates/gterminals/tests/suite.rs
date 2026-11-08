//! Integration suite for `gobby-terminals`.
//!
//! Each top-level test file is one module of this program, so the crate builds a
//! single integration test binary. A module that needs a feature carries its `cfg`
//! here, where its former `required-features` gated a separate binary.

// Shared helpers load once here; every test module reaches them through `super`.
#[path = "common/mod.rs"]
mod common;

#[path = "control_snapshot.rs"]
mod control_snapshot;

#[path = "family_cutover.rs"]
mod family_cutover;

#[path = "host_lifecycle.rs"]
mod host_lifecycle;

#[path = "leases.rs"]
mod leases;

#[path = "protocol_contract.rs"]
mod protocol_contract;

#[path = "reconcile.rs"]
mod reconcile;

#[path = "tmux_write.rs"]
mod tmux_write;

#[path = "write_coordinator.rs"]
mod write_coordinator;

#[path = "write_route.rs"]
mod write_route;

#[path = "write_runtimes.rs"]
mod write_runtimes;

#[path = "ws_golden.rs"]
mod ws_golden;
