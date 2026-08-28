//! Text selection and clipboard support.
//!
//! Selection lifecycle:
//!
//!   MouseDown in pane → Anchor recorded (no visual yet)
//!   MouseDrag         → Selection becomes active, cells highlighted
//!   MouseUp           → Selection finalized; optionally copied by the caller
//!   Next click / key  → A retained selection is cleared
//!
//! Double-click selects a word; the caller decides whether to copy it immediately.
//!
//! Rows are stored in screen-buffer coordinates instead of viewport-relative
//! coordinates. That keeps selection stable while the pane scrolls.

use ratatui::layout::Rect;
use std::{ffi::OsStr, io::Write};

use crate::layout::{PaneId, ScrollMetrics};

/// Current phase of a selection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    /// Mouse is down but hasn't moved yet. If released without
    /// moving, this was just a click — no selection created.
    Anchored,
    /// Mouse has moved from the anchor point. Cells are being highlighted.
    Dragging,
    /// Mouse released after dragging. Selection is visible and the gesture is
    /// complete. Clipboard policy is owned by the caller.
    Done,
}

/// A text selection within a terminal pane.
#[derive(Debug, Clone)]
pub struct Selection {
    /// Which pane the selection belongs to.
    pub pane_id: PaneId,
    /// Anchor position in screen-buffer coordinates (row, col).
    anchor: (u32, u16),
    /// Current/final position in screen-buffer coordinates (row, col).
    cursor: (u32, u16),
    /// Selection phase.
    phase: Phase,
}

impl Selection {
    /// Start a potential selection. This records the anchor but doesn't
    /// make anything visible yet — the user might just be clicking.
    pub fn anchor(
        pane_id: PaneId,
        viewport_row: u16,
        col: u16,
        metrics: Option<ScrollMetrics>,
    ) -> Self {
        let anchor = (absolute_row_for_viewport_row(viewport_row, metrics), col);
        Self {
            pane_id,
            anchor,
            cursor: anchor,
            phase: Phase::Anchored,
        }
    }

    /// Create an active selection from an explicit viewport-row range.
    pub(crate) fn range(
        pane_id: PaneId,
        viewport_row: u16,
        start_col: u16,
        end_col: u16,
        metrics: Option<ScrollMetrics>,
    ) -> Self {
        let row = absolute_row_for_viewport_row(viewport_row, metrics);
        Self {
            pane_id,
            anchor: (row, start_col),
            cursor: (row, end_col),
            phase: Phase::Dragging,
        }
    }

    pub(crate) fn line_range(
        pane_id: PaneId,
        anchor_row: u32,
        cursor_row: u32,
        end_col: u16,
    ) -> Self {
        let (anchor_col, cursor_col) = if anchor_row <= cursor_row {
            (0, end_col)
        } else {
            (end_col, 0)
        };
        Self {
            pane_id,
            anchor: (anchor_row, anchor_col),
            cursor: (cursor_row, cursor_col),
            phase: Phase::Dragging,
        }
    }

    pub(crate) fn absolute_row_for_viewport(
        viewport_row: u16,
        metrics: Option<ScrollMetrics>,
    ) -> u32 {
        absolute_row_for_viewport_row(viewport_row, metrics)
    }

    /// Convert the anchor's absolute row and pane-relative column back to
    /// screen coordinates. Adds the pane origin before clamping so the
    /// returned (screen_row, screen_col) can be compared directly against
    /// mouse screen positions.
    pub fn anchor_screen_pos(
        &self,
        pane_inner: Rect,
        metrics: Option<ScrollMetrics>,
    ) -> (u16, u16) {
        let viewport_row = viewport_row_for_absolute_row(self.anchor.0, metrics);
        // Convert pane-relative to screen coordinates, then clamp.
        let row = (viewport_row.saturating_add(pane_inner.y)).clamp(
            pane_inner.y,
            pane_inner.y + pane_inner.height.saturating_sub(1),
        );
        let col = (self.anchor.1.saturating_add(pane_inner.x)).clamp(
            pane_inner.x,
            pane_inner.x + pane_inner.width.saturating_sub(1),
        );
        (row, col)
    }

    /// Extend the selection as the mouse drags. Activates highlighting
    /// once the cursor moves to a different cell than the anchor.
    /// Screen coordinates are clamped to the pane boundary.
    pub fn drag(
        &mut self,
        screen_col: u16,
        screen_row: u16,
        pane_inner: Rect,
        metrics: Option<ScrollMetrics>,
    ) {
        let (viewport_row, col) = clamp_to_pane(screen_col, screen_row, pane_inner);
        self.cursor = (absolute_row_for_viewport_row(viewport_row, metrics), col);
        if self.cursor != self.anchor {
            self.phase = Phase::Dragging;
        }
    }

    /// Finalize the selection. Returns the selected range if the user
    /// actually dragged (not just clicked). Returns None for plain clicks.
    pub fn finish(&mut self) -> bool {
        if self.phase == Phase::Dragging {
            self.phase = Phase::Done;
            true
        } else {
            false
        }
    }

    /// Whether this selection should be rendered (highlight visible).
    pub fn is_visible(&self) -> bool {
        self.phase == Phase::Dragging || self.phase == Phase::Done
    }

    /// Whether this selection was already finalized.
    pub fn is_finalized(&self) -> bool {
        self.phase == Phase::Done
    }

    /// Whether the user just clicked without dragging (not a selection).
    pub fn was_just_click(&self) -> bool {
        self.phase == Phase::Anchored
    }

    /// Whether the user just clicked without dragging (not a selection).
    pub fn is_just_click(&self) -> bool {
        self.phase == Phase::Anchored
    }

    /// Force the selection into Dragging phase, used when the mouse
    /// has moved off the anchor cell but drag() couldn't transition
    /// because the cursor was clamped to the same cell as the anchor.
    pub fn force_dragging(&mut self) {
        if self.phase == Phase::Anchored {
            self.phase = Phase::Dragging;
        }
    }

    /// Whether the pointer is still down and the selection can keep extending.
    pub fn is_in_progress(&self) -> bool {
        matches!(self.phase, Phase::Anchored | Phase::Dragging)
    }

    /// Whether the user is actively dragging (cursor moved from anchor).
    pub fn is_dragging(&self) -> bool {
        self.phase == Phase::Dragging
    }

    /// Returns (start, end) in reading order (top-left to bottom-right).
    fn ordered(&self) -> ((u32, u16), (u32, u16)) {
        let (ar, ac) = self.anchor;
        let (cr, cc) = self.cursor;
        if ar < cr || (ar == cr && ac <= cc) {
            ((ar, ac), (cr, cc))
        } else {
            ((cr, cc), (ar, ac))
        }
    }

    pub(crate) fn ordered_cells(&self) -> ((u32, u16), (u32, u16)) {
        self.ordered()
    }

    /// Check whether a pane-relative cell (row, col) is inside the selection.
    pub fn contains(&self, viewport_row: u16, col: u16, metrics: Option<ScrollMetrics>) -> bool {
        if !self.is_visible() {
            return false;
        }
        let row = absolute_row_for_viewport_row(viewport_row, metrics);
        let ((sr, sc), (er, ec)) = self.ordered();
        if row < sr || row > er {
            return false;
        }
        if sr == er {
            col >= sc && col <= ec
        } else if row == sr {
            col >= sc
        } else if row == er {
            col <= ec
        } else {
            true
        }
    }
}

fn viewport_top_row(metrics: Option<ScrollMetrics>) -> u32 {
    metrics
        .map(|metrics| {
            metrics
                .max_offset_from_bottom
                .saturating_sub(metrics.offset_from_bottom)
        })
        .unwrap_or(0) as u32
}

fn absolute_row_for_viewport_row(viewport_row: u16, metrics: Option<ScrollMetrics>) -> u32 {
    viewport_top_row(metrics) + u32::from(viewport_row)
}

fn viewport_row_for_absolute_row(absolute_row: u32, metrics: Option<ScrollMetrics>) -> u16 {
    absolute_row
        .saturating_sub(viewport_top_row(metrics))
        .try_into()
        .unwrap_or(0)
}

fn clamp_to_pane(screen_col: u16, screen_row: u16, pane_inner: Rect) -> (u16, u16) {
    let clamped_col = screen_col.clamp(
        pane_inner.x,
        pane_inner.x + pane_inner.width.saturating_sub(1),
    );
    let clamped_row = screen_row.clamp(
        pane_inner.y,
        pane_inner.y + pane_inner.height.saturating_sub(1),
    );
    (clamped_row - pane_inner.y, clamped_col - pane_inner.x)
}

fn osc52_sequence(bytes: &[u8]) -> String {
    use base64::Engine;
    let encoded = base64::engine::general_purpose::STANDARD.encode(bytes);
    format!("\x1b]52;c;{encoded}\x07")
}

fn contains_wsl_marker(value: &str) -> bool {
    let lower = value.to_ascii_lowercase();
    lower.contains("microsoft") || lower.contains("wsl2") || lower.contains("-wsl")
}

fn is_wsl_for_env(
    os_release: Option<&str>,
    proc_version: Option<&str>,
    wsl_distro_name: Option<&OsStr>,
    wsl_interop: Option<&OsStr>,
    runtime_marker_exists: bool,
) -> bool {
    wsl_distro_name.is_some()
        || wsl_interop.is_some()
        || os_release.is_some_and(contains_wsl_marker)
        || proc_version.is_some_and(contains_wsl_marker)
        || runtime_marker_exists
}

fn is_wsl() -> bool {
    let os_release = std::fs::read_to_string("/proc/sys/kernel/osrelease").ok();
    let proc_version = std::fs::read_to_string("/proc/version").ok();
    is_wsl_for_env(
        os_release.as_deref(),
        proc_version.as_deref(),
        std::env::var_os("WSL_DISTRO_NAME").as_deref(),
        std::env::var_os("WSL_INTEROP").as_deref(),
        std::path::Path::new("/run/WSL").exists()
            || std::path::Path::new("/proc/sys/fs/binfmt_misc/WSLInterop").exists(),
    )
}

fn should_prefer_osc52_for_env(
    ssh_connection: Option<&OsStr>,
    ssh_tty: Option<&OsStr>,
    vscode_ipc_hook_cli: Option<&OsStr>,
    wsl: bool,
) -> bool {
    ssh_connection.is_some() || ssh_tty.is_some() || vscode_ipc_hook_cli.is_some() || wsl
}

fn should_prefer_osc52() -> bool {
    should_prefer_osc52_for_env(
        std::env::var_os("SSH_CONNECTION").as_deref(),
        std::env::var_os("SSH_TTY").as_deref(),
        std::env::var_os("VSCODE_IPC_HOOK_CLI").as_deref(),
        is_wsl(),
    )
}

/// Write clipboard bytes to the system clipboard via native platform tools or OSC 52.
///
/// OSC 52 format: `ESC ] 52 ; c ; <base64> BEL`
///
/// Some terminals still only honor BEL-terminated OSC 52 writes, so gterm
/// emits BEL here even though ST works in newer emulators.
pub fn write_osc52_bytes(bytes: &[u8]) {
    if !should_prefer_osc52() && crate::platform::write_clipboard(bytes) {
        return;
    }

    let sequence = osc52_sequence(bytes);
    let _ = std::io::stdout().write_all(sequence.as_bytes());
    let _ = std::io::stdout().flush();
}

#[cfg(test)]
#[path = "selection/tests.rs"]
mod tests;
