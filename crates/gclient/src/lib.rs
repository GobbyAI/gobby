//! Gobby terminal workspace client (`gclient`).

pub mod app;
pub mod copy_mode;
pub mod daemon;
pub mod frame_source;
pub mod input;
pub mod persist;
pub mod teardown;
pub mod theme;
pub mod ui;
pub mod views;

pub use app::Workspace;
pub use daemon::{decode_message, encode_message, GOLDEN_NAMES, TERMINAL_WS_SAFE_INTEGER_MAX};
pub use frame_source::{AttachLocator, FrameError, FrameSource, ScriptedFrameSource};
