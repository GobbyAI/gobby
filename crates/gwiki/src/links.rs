//! Wikilink extraction and target normalization.
//!
//! The implementation lives in `gobby_core::vault::links` (#17514) so gcode
//! and gwiki resolve link targets with identical grammar; this module
//! re-exports the pieces gwiki consumes under the crate-local path every
//! consumer already uses.

pub use gobby_core::vault::links::{LinkKind, WikiLink, canonical_target_key, extract_links};

/// Entity-shaped keys (no path separator) are what upkeep clusters into
/// entity pages; path-shaped targets — including `knowledge/sources/...`
/// digest keys — can never converge that way. Upkeep's cluster accumulation
/// and librarian's broken-link classification must share this predicate, or
/// upkeep mints junk pages librarian already wrote off as repair debt
/// (#17652).
pub(crate) fn is_entity_key(key: &str) -> bool {
    !key.is_empty() && !key.contains('/')
}
