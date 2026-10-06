//! Shared wire protocol and presentation encoding code.

#[cfg(unix)]
pub(crate) mod render_ansi;
mod wire;

pub use wire::*;
