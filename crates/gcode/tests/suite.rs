//! Integration suite for `gobby-code`.
//!
//! Each top-level test file is one module of this program, so the crate builds a
//! single integration test binary. A module that needs a feature carries its `cfg`
//! here, where its former `required-features` gated a separate binary.

#[path = "cli_schema_identity.rs"]
mod cli_schema_identity;

#[cfg(feature = "test-support")]
#[path = "concurrent_vector_grant.rs"]
mod concurrent_vector_grant;

#[path = "contract.rs"]
mod contract;

#[cfg(feature = "ai")]
#[path = "effective_config.rs"]
mod effective_config;

#[path = "evidence.rs"]
mod evidence;

#[path = "facade_boundary.rs"]
mod facade_boundary;

#[cfg(feature = "test-support")]
#[path = "file_lock_concurrency.rs"]
mod file_lock_concurrency;

#[path = "grant_errors.rs"]
mod grant_errors;

#[path = "release_workflows.rs"]
mod release_workflows;

#[path = "workflow_security.rs"]
mod workflow_security;
