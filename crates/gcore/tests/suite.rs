//! Integration suite for `gobby-core`.
//!
//! Each top-level test file is one module of this program, so the crate builds a
//! single integration test binary. A module that needs a feature carries its `cfg`
//! here, where its former `required-features` gated a separate binary.

#[path = "bootstrap_mode.rs"]
mod bootstrap_mode;

#[path = "bootstrap_run_modes.rs"]
mod bootstrap_run_modes;

#[path = "domain_error.rs"]
mod domain_error;

#[path = "domain_error_http.rs"]
mod domain_error_http;

#[path = "domain_error_internal.rs"]
mod domain_error_internal;

#[path = "mcp_contract.rs"]
mod mcp_contract;

#[path = "public_boundary.rs"]
mod public_boundary;
