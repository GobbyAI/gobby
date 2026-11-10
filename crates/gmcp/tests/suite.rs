//! Integration suite for `gobby-mcp`.
//!
//! Each top-level test file is one module of this program, so the crate builds a
//! single integration test binary. A module that needs a feature carries its `cfg`
//! here, where its former `required-features` gated a separate binary. Shared
//! helpers are declared once here, so each module imports them from `crate`.

mod stub_daemon;

#[path = "call_tool.rs"]
mod call_tool;

#[path = "daemon_start.rs"]
mod daemon_start;

#[path = "dialer.rs"]
mod dialer;

#[path = "dialer_restart.rs"]
mod dialer_restart;

#[path = "server.rs"]
mod server;
