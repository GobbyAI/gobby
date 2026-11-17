// upstream: none
//! The effective prefix chord.
//!
//! The default prefix is `ctrl+b`. A `prefix` key in the keymap override
//! file selects a different chord.

/// gclient's default prefix chord.
pub const DEFAULT_PREFIX: &str = "ctrl+b";

/// The default prefix, before user overrides.
pub fn default_prefix() -> &'static str {
    DEFAULT_PREFIX
}
