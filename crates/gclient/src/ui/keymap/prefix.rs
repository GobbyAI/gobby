// upstream: none (herdr v0.8.0 hard-codes `ctrl+b` in src/config/keybinds.rs)
//! The effective prefix chord.
//!
//! herdr's prefix is `ctrl+b`, which is also tmux's. Gobby spawns no tmux
//! (#22856), but gclient can still run inside a pane the user started, and
//! that outer tmux consumes every `ctrl+b` before the client sees it, so
//! under an outer tmux the default moves to `ctrl+]`. A `prefix` key in the
//! keymap override file wins over either default.

/// herdr's prefix, and gclient's whenever no outer tmux eats it.
pub const HERDR_PREFIX: &str = "ctrl+b";

/// The prefix under an outer tmux, whose own prefix is `ctrl+b`.
pub const NESTED_PREFIX: &str = "ctrl+]";

/// The default prefix for this launch: `nested` is whether an outer tmux
/// client owns the terminal (`tmux_identity::current()` at startup).
pub fn default_prefix(nested: bool) -> &'static str {
    if nested {
        NESTED_PREFIX
    } else {
        HERDR_PREFIX
    }
}
