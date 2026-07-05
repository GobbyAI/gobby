//! Wikilink extraction and target normalization.
//!
//! The implementation lives in `gobby_core::vault::links` (#17514) so gcode
//! and gwiki resolve link targets with identical grammar; this module
//! re-exports the pieces gwiki consumes under the crate-local path every
//! consumer already uses.

pub use gobby_core::vault::links::{WikiLink, canonical_target_key, extract_links};
