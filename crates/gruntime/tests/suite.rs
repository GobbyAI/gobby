//! Integration suite for `gobby-runtime`.
//!
//! Each top-level test file is one module of this program, so the crate builds a
//! single integration test binary. A module that needs a feature carries its `cfg`
//! here, where its former `required-features` gated a separate binary.

#[path = "capabilities.rs"]
mod capabilities;

#[path = "config.rs"]
mod config;

#[path = "grant.rs"]
mod grant;

#[path = "handshake.rs"]
mod handshake;

#[path = "secrets.rs"]
mod secrets;

#[path = "verify.rs"]
mod verify;
