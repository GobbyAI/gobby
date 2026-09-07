//! Imported herdr v0.8.0 UI chrome, rewired to Gobby roster and attention.
//!
//! `chrome::Chrome` is the UI view-state the run loop owns; `render_workspace`
//! composes the imported modules from a `WorkspaceView` plus that state.
//! See `UPSTREAM.md` for the accept/reject map and `keymap` for provenance.

pub mod chrome;
pub mod chrome_render;
pub mod dialogs;
pub mod hit;
pub mod keybind_help;
pub mod keymap;
pub mod navigator;
pub mod pane_layout;
pub mod panes;
pub mod scrollbar;
pub mod settings;
pub mod sidebar;
pub mod sidebar_rows;
pub mod sidebar_tokens;
pub mod status;
pub mod tab_surface;
pub mod tabs;
pub mod text;
pub mod widgets;

pub use chrome::{Chrome, Mode, RowState, WorkspaceView};
pub use chrome_render::{render_workspace, render_workspace_with};
pub use keymap::{Action, Keymap, KeymapError};
