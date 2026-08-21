//! Terminal keep-set for the Gobby `gterm` host and `gclient` workspace.
//!
//! `src/ghostty/` and `src/pane/` compile only with the `vt-engine` feature.
//! Wire protocol, input encoding, layout, selection, and theme modules stay
//! feature-free so default-feature builds remain Zig-free.

#![allow(dead_code, unused_imports, private_interfaces, clippy::all)]

pub(crate) const GTERM_ENV_VAR: &str = "GTERM_ENV";
pub(crate) const GTERM_ENV_VALUE: &str = "1";

pub mod host;
pub mod input;
pub mod ipc;
pub mod layout;
pub mod platform;
pub mod protocol;
pub mod pty;
pub mod raw_input;
pub mod render_prof;
#[cfg(feature = "vt-engine")]
pub mod runtime;
pub mod selection;
pub mod terminal_modes;
pub mod terminal_theme;

#[cfg(feature = "vt-engine")]
pub mod ghostty;
#[cfg(feature = "vt-engine")]
pub mod kitty_graphics;
#[cfg(feature = "vt-engine")]
pub mod pane;
