//! Integration suite for `gobby-hooks`.
//!
//! Each top-level test file is one module of this program, so the crate builds a
//! single integration test binary. A module that needs a feature carries its `cfg`
//! here, where its former `required-features` gated a separate binary.

#[path = "cli_schema_identity.rs"]
mod cli_schema_identity;

#[path = "contract.rs"]
mod contract;

#[path = "inbox_fallback.rs"]
mod inbox_fallback;
