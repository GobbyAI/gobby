// upstream: none
//! The effective prefix chord.
//!
//! The default prefix is `ctrl+b`, which is also tmux's. Gobby spawns no tmux
//! (#22856), but gclient can still run inside a pane the user started, and
//! that outer tmux consumes every `ctrl+b` before the client sees it, so
//! under an outer tmux the default moves to `ctrl+]`. A `prefix` key in the
//! keymap override file wins over either default.

/// gclient's prefix whenever no outer tmux eats it.
pub const DEFAULT_PREFIX: &str = "ctrl+b";

/// The prefix under an outer tmux, whose own prefix is `ctrl+b`.
pub const NESTED_PREFIX: &str = "ctrl+]";

/// The default prefix for this launch: `nested` is whether an outer tmux
/// client owns the terminal (`tmux_identity::current()` at startup).
pub fn default_prefix(nested: bool) -> &'static str {
    if nested {
        NESTED_PREFIX
    } else {
        DEFAULT_PREFIX
    }
}
