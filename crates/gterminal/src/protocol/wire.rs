//! Wire protocol for gterm server/client communication.
//!
//! Defines the message types, framing, version negotiation, and safety
//! constraints for the binary protocol over Unix domain sockets.

// ---------------------------------------------------------------------------
// Protocol constants
// ---------------------------------------------------------------------------

/// Current protocol version. Bumped when wire format changes incompatibly.
pub const PROTOCOL_VERSION: u32 = 1;

/// Maximum allowed frame payload size (2 MB). Frames larger than this are
/// rejected to prevent denial-of-service via oversized length prefixes.
pub const MAX_FRAME_SIZE: usize = 2 * 1024 * 1024;

/// Bytes reserved for a `ServerMessage::Frame` envelope around the cell grid.
/// Measured as the encoded size of an empty 0×0 `FrameData` plus enum tag.
pub const FRAME_HEADER: usize = 64;

/// Encoded size of the most expensive admitted cell under `FrameData` bincode
/// and under `TerminalAnsi` with alternating attributes. See
/// `worst_cell_bytes` in `wire_types`.
pub const WORST_CELL_BYTES: usize = 96;

/// Maximum cells in one keyframe: floor((MAX_FRAME_SIZE - FRAME_HEADER) / WORST_CELL_BYTES).
pub const MAX_CELLS: usize = (MAX_FRAME_SIZE - FRAME_HEADER) / WORST_CELL_BYTES;

pub const MIN_ROWS: u16 = 1;
pub const MIN_COLS: u16 = 1;
pub const MAX_ROWS: u16 = 1024;
pub const MAX_COLS: u16 = 1024;

/// OSC / tmux title cap (UTF-8 bytes) on a code-point boundary.
pub const TITLE_MAX_BYTES: usize = 1024;

/// Raw UTF-8 cap for control `write` / `paste` payloads.
pub const MAX_WRITE_BYTES: usize = 1024 * 1024;

pub const DELTA_QUEUE_ENTRIES: usize = 64;
pub const DELTA_QUEUE_BYTES: usize = MAX_FRAME_SIZE;
pub const CONTROL_QUEUE_ENTRIES: usize = 16;
pub const CONTROL_QUEUE_BYTES: usize = 64 * 1024;
pub const CONTROL_DELIVERY_DEADLINE_MS: u64 = 2_000;
pub const DELTA_LAG_TIMEOUT_MS: u64 = 5_000;
pub const SNAPSHOT_DEFAULT_MAX_BYTES: usize = 256 * 1024;
pub const SNAPSHOT_DEFAULT_MAX_LINES: usize = 500;
pub const EVENT_QUEUE_ENTRIES: usize = 256;
pub const EVENT_QUEUE_BYTES: usize = 256 * 1024;
pub const OPERATION_LEDGER_SIZE: usize = 64;
pub const LIFECYCLE_RESERVED_SLOTS: usize = 4;

pub const DEFAULT_MAX_ATTACHMENTS_PER_TERMINAL: u32 = 8;
pub const MIN_MAX_ATTACHMENTS_PER_TERMINAL: u32 = 1;
pub const MAX_MAX_ATTACHMENTS_PER_TERMINAL: u32 = 8;
pub const DEFAULT_MAX_ATTACHMENTS_TOTAL: u32 = 128;
pub const MIN_MAX_ATTACHMENTS_TOTAL: u32 = 4;
pub const MAX_MAX_ATTACHMENTS_TOTAL: u32 = 128;
pub const DEFAULT_MAX_ATTACHED_TERMINALS: u32 = 64;
pub const MIN_MAX_ATTACHED_TERMINALS: u32 = 1;
pub const MAX_MAX_ATTACHED_TERMINALS: u32 = 64;
pub const DEFAULT_NATIVE_SCROLLBACK_MAX_LINES: u32 = 10_000;
pub const MIN_NATIVE_SCROLLBACK_MAX_LINES: u32 = 500;
pub const MAX_NATIVE_SCROLLBACK_MAX_LINES: u32 = 50_000;
pub const DEFAULT_NATIVE_SCROLLBACK_MAX_BYTES: u32 = 8 * 1024 * 1024;
pub const MIN_NATIVE_SCROLLBACK_MAX_BYTES: u32 = 256 * 1024;
pub const MAX_NATIVE_SCROLLBACK_MAX_BYTES: u32 = 32 * 1024 * 1024;
pub const DEFAULT_TMUX_ATTACH_HISTORY_LINES: u32 = 500;
pub const MIN_TMUX_ATTACH_HISTORY_LINES: u32 = 1;
pub const MAX_TMUX_ATTACH_HISTORY_LINES: u32 = 2_000;
pub const DEFAULT_TMUX_ATTACH_HISTORY_MAX_BYTES: u32 = 256 * 1024;
pub const MIN_TMUX_ATTACH_HISTORY_MAX_BYTES: u32 = 1024;
pub const MAX_TMUX_ATTACH_HISTORY_MAX_BYTES: u32 = 256 * 1024;
pub const DEFAULT_TMUX_POLL_INTERVAL_MS: u32 = 150;
pub const MIN_TMUX_POLL_INTERVAL_MS: u32 = 50;
pub const MAX_TMUX_POLL_INTERVAL_MS: u32 = 5_000;
pub const DEFAULT_TMUX_POLL_BACKOFF_CEILING_MS: u32 = 5_000;
pub const MIN_TMUX_POLL_BACKOFF_CEILING_MS: u32 = 150;
pub const MAX_TMUX_POLL_BACKOFF_CEILING_MS: u32 = 30_000;

/// Maximum allowed server-to-client frame payload when Kitty graphics are enabled.
/// Normal traffic keeps `MAX_FRAME_SIZE`; this larger cap is only for explicit
/// image payloads that are naturally much larger after base64 encoding.
pub const MAX_GRAPHICS_FRAME_SIZE: usize = 32 * 1024 * 1024;

/// Maximum clipboard image payload size for remote paste bridging.
pub const MAX_CLIPBOARD_IMAGE_PAYLOAD: usize = 16 * 1024 * 1024;

/// Length of the u32 little-endian length prefix in bytes.
const LENGTH_PREFIX_BYTES: usize = 4;

// ---------------------------------------------------------------------------
// Client → Server messages
// ---------------------------------------------------------------------------

#[path = "wire_codec.rs"]
mod wire_codec;
#[path = "wire_types.rs"]
mod wire_types;
pub use wire_codec::*;
pub use wire_types::*;

#[cfg(test)]
#[path = "wire/tests.rs"]
mod tests;
