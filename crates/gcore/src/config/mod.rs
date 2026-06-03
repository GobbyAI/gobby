//! Shared configuration-resolution boundary.
//!
//! This module is the public home for lightweight configuration contracts that
//! are shared across Gobby Rust crates. Concrete service resolution is added in
//! focused follow-up modules so this baseline crate remains small.

mod resolve;
mod types;

pub use resolve::*;
pub use types::*;

#[cfg(test)]
pub(crate) static TEST_ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[cfg(test)]
mod tests;
