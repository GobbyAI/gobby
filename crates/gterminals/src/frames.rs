//! Frame-socket client: 4-byte little-endian length prefix + bincode 2
//! (`config::standard()`) payloads, mirroring gterm's `protocol` module.
//!
//! Variant and field order is the wire format. Every type below must match
//! `crates/gterminal/src/protocol/wire_types.rs` exactly; the golden corpus
//! test fails on any drift.

use std::collections::VecDeque;

use serde::{Deserialize, Serialize, de::DeserializeOwned};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_FRAME_SIZE: usize = 2 * 1024 * 1024;
pub const MAX_WRITE_BYTES: usize = 1024 * 1024;
pub const DELTA_QUEUE_ENTRIES: usize = 64;
pub const DELTA_QUEUE_BYTES: usize = MAX_FRAME_SIZE;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum RenderEncoding {
    SemanticFrame,
    TerminalAnsi,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct TmuxClientIdentity {
    pub socket_path: String,
    pub server_pid: i32,
    pub server_start_time: i64,
    pub pane_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct PaneLocator {
    pub socket_path: String,
    pub server_pid: i32,
    pub server_start_time: i64,
    pub pane_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct RgbColor {
    pub r: u8,
    pub g: u8,
    pub b: u8,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct ThemeDeclaration {
    pub foreground: Option<RgbColor>,
    pub background: Option<RgbColor>,
    pub palette: Vec<(u8, RgbColor)>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct PaneModes {
    pub cursor_visible: bool,
    pub cursor_very_visible: bool,
    pub cursor_shape: u8,
    pub cursor_blinking: bool,
    pub cursor_colour: String,
    pub alternate_on: bool,
    pub keypad_cursor: bool,
    pub keypad: bool,
    pub bracket_paste: bool,
    pub mouse_standard: bool,
    pub mouse_button: bool,
    pub mouse_any: bool,
    pub mouse_all: bool,
    pub mouse_sgr: bool,
    pub mouse_utf8: bool,
    pub wrap: bool,
    pub origin: bool,
    pub insert: bool,
    pub scroll_region_upper: u16,
    pub scroll_region_lower: u16,
    pub pane_in_mode: bool,
    #[serde(default)]
    pub kitty_keyboard_flags: u16,
}

/// Client → host messages. Tags 1–3 are gterm's reserved legacy verbs: they
/// keep their positions so later tags stay byte-stable, and the host refuses
/// them, so this client never sends them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientMessage {
    Hello {
        version: u32,
        encoding: RenderEncoding,
        local_token: String,
        cols: u16,
        rows: u16,
        #[serde(default)]
        tmux_identity: Option<TmuxClientIdentity>,
    },
    LegacyInput {
        data: Vec<u8>,
    },
    LegacyClipboard {
        extension: String,
        data: Vec<u8>,
    },
    LegacyResize {
        cols: u16,
        rows: u16,
        cell_width_px: u32,
        cell_height_px: u32,
    },
    Detach,
    AttachTerminal {
        host_terminal_id: String,
        #[serde(default)]
        reservation_id: Option<String>,
        #[serde(default)]
        locator: Option<PaneLocator>,
    },
    SetViewport {
        rows: u16,
        cols: u16,
    },
    SetScrollOffset {
        rows_from_live_edge: u32,
    },
    BindAttachment {
        attachment_id: String,
    },
    Input {
        data: Vec<u8>,
    },
    Paste {
        text: String,
    },
    ReadText {
        start_rows_from_live_edge: u32,
        start_col: u16,
        end_rows_from_live_edge: u32,
        end_col: u16,
    },
    SetTerminalTheme {
        theme: ThemeDeclaration,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CellData {
    pub symbol: String,
    pub fg: u32,
    pub bg: u32,
    pub modifier: u16,
    pub skip: bool,
    pub hyperlink: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CursorState {
    pub x: u16,
    pub y: u16,
    pub visible: bool,
    #[serde(default)]
    pub shape: u8,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FrameData {
    pub cells: Vec<CellData>,
    pub width: u16,
    pub height: u16,
    pub cursor: Option<CursorState>,
    pub hyperlinks: Vec<String>,
    pub graphics: Vec<u8>,
    #[serde(default)]
    pub modes: PaneModes,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TerminalFrame {
    pub seq: u64,
    pub width: u16,
    pub height: u16,
    pub full: bool,
    pub bytes: Vec<u8>,
}

/// Host → client messages.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ServerMessage {
    Welcome {
        host_epoch: String,
    },
    Frame(FrameData),
    Terminal(TerminalFrame),
    Graphics {
        bytes: Vec<u8>,
    },
    AttachHistory {
        text: String,
        truncated: bool,
        dropped_bytes: u64,
        total_bytes: u64,
    },
    ScrollOffsetApplied {
        applied_rows: u32,
        max_rows: u32,
    },
    TerminalExited {
        host_terminal_id: String,
        #[serde(default)]
        exit_code: Option<i32>,
    },
    Error {
        code: String,
        message: Option<String>,
    },
    Attached {
        created: bool,
        host_terminal_id: String,
    },
    InputRefused {
        code: String,
    },
    TextRead {
        text: String,
        truncated: bool,
    },
}

#[derive(Debug, thiserror::Error)]
pub enum FrameError {
    #[error("frame of {claimed} bytes exceeds the {max}-byte ceiling")]
    Oversized { claimed: usize, max: usize },
    #[error("frame length {claimed} does not match the {actual} payload bytes present")]
    Length { claimed: usize, actual: usize },
    #[error("bincode: {0}")]
    Bincode(String),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error("frame stream closed")]
    Closed,
    #[error("host epoch changed: expected {expected}, got {actual}")]
    EpochChanged { expected: String, actual: String },
    #[error("expected welcome, got {0}")]
    Refused(String),
    #[error("frame queue overflow")]
    Lag,
}

const LENGTH_PREFIX: usize = 4;

/// Encode one framed message exactly as gterm's `write_message` does.
///
/// gterm's writer only refuses payloads past `u32::MAX`; this client also
/// refuses anything past `MAX_FRAME_SIZE`, because the host would reject the
/// frame and drop the stream.
pub fn encode_frame<M: Serialize>(message: &M) -> Result<Vec<u8>, FrameError> {
    let payload = bincode::serde::encode_to_vec(message, bincode::config::standard())
        .map_err(|err| FrameError::Bincode(err.to_string()))?;
    let claimed = checked_len(payload.len())?;
    let mut frame = Vec::with_capacity(LENGTH_PREFIX + payload.len());
    frame.extend_from_slice(&claimed.to_le_bytes());
    frame.extend_from_slice(&payload);
    Ok(frame)
}

/// Decode one complete framed message (prefix included).
pub fn decode_frame<M: DeserializeOwned>(frame: &[u8]) -> Result<M, FrameError> {
    let prefix: [u8; LENGTH_PREFIX] = frame
        .get(..LENGTH_PREFIX)
        .and_then(|bytes| bytes.try_into().ok())
        .ok_or(FrameError::Length {
            claimed: LENGTH_PREFIX,
            actual: frame.len(),
        })?;
    let claimed = validate_len(prefix)?;
    let payload = &frame[LENGTH_PREFIX..];
    if payload.len() != claimed {
        return Err(FrameError::Length {
            claimed,
            actual: payload.len(),
        });
    }
    decode_payload(payload)
}

fn checked_len(len: usize) -> Result<u32, FrameError> {
    if len > MAX_FRAME_SIZE {
        return Err(FrameError::Oversized {
            claimed: len,
            max: MAX_FRAME_SIZE,
        });
    }
    // MAX_FRAME_SIZE fits in u32, so the conversion cannot fail here.
    u32::try_from(len).map_err(|_| FrameError::Oversized {
        claimed: len,
        max: MAX_FRAME_SIZE,
    })
}

fn validate_len(prefix: [u8; LENGTH_PREFIX]) -> Result<usize, FrameError> {
    let claimed = u32::from_le_bytes(prefix) as usize;
    if claimed > MAX_FRAME_SIZE {
        return Err(FrameError::Oversized {
            claimed,
            max: MAX_FRAME_SIZE,
        });
    }
    Ok(claimed)
}

fn decode_payload<M: DeserializeOwned>(payload: &[u8]) -> Result<M, FrameError> {
    let (message, consumed) =
        bincode::serde::decode_from_slice(payload, bincode::config::standard())
            .map_err(|err| FrameError::Bincode(err.to_string()))?;
    if consumed != payload.len() {
        return Err(FrameError::Length {
            claimed: payload.len(),
            actual: consumed,
        });
    }
    Ok(message)
}

/// Bounded receive-side delta queue with gterm's entry and byte ceilings.
/// Bytes are counted as encoded frame length; overflow is a lag error.
#[derive(Debug, Default)]
pub struct DeltaQueue {
    entries: VecDeque<(usize, ServerMessage)>,
    bytes: usize,
}

impl DeltaQueue {
    pub fn push(&mut self, message: ServerMessage) -> Result<(), FrameError> {
        let size = encode_frame(&message)?.len();
        if self.entries.len() >= DELTA_QUEUE_ENTRIES || self.bytes + size > DELTA_QUEUE_BYTES {
            return Err(FrameError::Lag);
        }
        self.bytes += size;
        self.entries.push_back((size, message));
        Ok(())
    }

    pub fn pop(&mut self) -> Option<ServerMessage> {
        let (size, message) = self.entries.pop_front()?;
        self.bytes -= size;
        Some(message)
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// A frame-socket client over any byte stream.
#[derive(Debug)]
pub struct FrameClient<S> {
    stream: S,
}

impl<S: AsyncRead + AsyncWrite + Unpin> FrameClient<S> {
    pub fn new(stream: S) -> Self {
        Self { stream }
    }

    /// Send `Hello` and require a `Welcome` from `expected_epoch`.
    pub async fn handshake(
        &mut self,
        expected_epoch: &str,
        local_token: &str,
        encoding: RenderEncoding,
        cols: u16,
        rows: u16,
        tmux_identity: Option<TmuxClientIdentity>,
    ) -> Result<(), FrameError> {
        self.send(&ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding,
            local_token: local_token.to_owned(),
            cols,
            rows,
            tmux_identity,
        })
        .await?;
        match self.recv().await? {
            ServerMessage::Welcome { host_epoch } if host_epoch == expected_epoch => Ok(()),
            ServerMessage::Welcome { host_epoch } => Err(FrameError::EpochChanged {
                expected: expected_epoch.to_owned(),
                actual: host_epoch,
            }),
            ServerMessage::Error { code, .. } => Err(FrameError::Refused(code)),
            other => Err(FrameError::Refused(format!("{other:?}"))),
        }
    }

    pub async fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError> {
        let frame = encode_frame(message)?;
        self.stream.write_all(&frame).await?;
        self.stream.flush().await?;
        Ok(())
    }

    /// Read one frame; an oversized prefix is refused before the payload is read.
    pub async fn recv(&mut self) -> Result<ServerMessage, FrameError> {
        let mut prefix = [0; LENGTH_PREFIX];
        read_exact(&mut self.stream, &mut prefix).await?;
        let mut payload = vec![0; validate_len(prefix)?];
        read_exact(&mut self.stream, &mut payload).await?;
        decode_payload(&payload)
    }
}

async fn read_exact<S: AsyncRead + Unpin>(
    stream: &mut S,
    buf: &mut [u8],
) -> Result<(), FrameError> {
    match stream.read_exact(buf).await {
        Ok(_) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::UnexpectedEof => Err(FrameError::Closed),
        Err(err) => Err(err.into()),
    }
}
