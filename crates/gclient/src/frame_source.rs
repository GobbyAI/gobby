//! Frame streams resolve only through this trait. Socket dials stay here.

use gobby_core::local_token::{read_local_cli_token, read_local_cli_token_for};
use gobby_terminal::protocol::{
    read_message, write_message, ClientMessage, RenderEncoding, ServerMessage, MAX_FRAME_SIZE,
    PROTOCOL_VERSION,
};
use std::io::{self, ErrorKind};
use std::path::{Path, PathBuf};
use thiserror::Error;

#[derive(Debug, Clone)]
pub struct AttachLocator {
    pub backend: String,
    pub frame_host_epoch: String,
    pub host_terminal_id: String,
    pub socket_path: String,
    pub pane_id: Option<String>,
}

#[derive(Debug, Error)]
pub enum FrameError {
    #[error("host epoch changed: expected {expected}, got {actual}")]
    HostEpochChanged { expected: String, actual: String },
    #[error("frame eof")]
    Eof,
    #[error("lag")]
    Lag,
    #[error("{0}")]
    Io(#[from] io::Error),
    #[error("{0}")]
    Other(String),
}

pub trait FrameSource {
    fn connect(
        &mut self,
        locator: &AttachLocator,
        cols: u16,
        rows: u16,
    ) -> Result<String, FrameError>;
    fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError>;
    fn sent_attach(&self) -> bool;
    fn sent_host_input(&self) -> bool;
    fn sent_resize(&self) -> bool;
    fn sent_mouse_report(&self) -> bool;
    fn sent_tiocswinsz(&self) -> bool;
    fn last_client_message(&self) -> Option<ClientMessage>;
}

#[derive(Debug, Default)]
pub struct ScriptedFrameSource {
    welcome_epoch: Option<String>,
    sent: Vec<ClientMessage>,
    sent_attach: bool,
}

impl ScriptedFrameSource {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set_welcome_epoch(&mut self, epoch: impl Into<String>) {
        self.welcome_epoch = Some(epoch.into());
    }

    pub fn sent_attach(&self) -> bool {
        self.sent_attach
    }

    pub fn sent_host_input(&self) -> bool {
        false
    }

    pub fn sent_resize(&self) -> bool {
        self.sent
            .iter()
            .any(|m| matches!(m, ClientMessage::LegacyResize { .. }))
    }

    pub fn sent_mouse_report(&self) -> bool {
        false
    }

    pub fn sent_tiocswinsz(&self) -> bool {
        false
    }

    pub fn last_client_message(&self) -> Option<ClientMessage> {
        self.sent.last().cloned()
    }
}

impl FrameSource for ScriptedFrameSource {
    fn connect(
        &mut self,
        locator: &AttachLocator,
        _cols: u16,
        _rows: u16,
    ) -> Result<String, FrameError> {
        let actual = self
            .welcome_epoch
            .clone()
            .unwrap_or_else(|| locator.frame_host_epoch.clone());
        if locator.frame_host_epoch != actual {
            return Err(FrameError::HostEpochChanged {
                expected: locator.frame_host_epoch.clone(),
                actual,
            });
        }
        Ok(actual)
    }

    fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError> {
        if matches!(message, ClientMessage::AttachTerminal { .. }) {
            self.sent_attach = true;
        }
        self.sent.push(message.clone());
        Ok(())
    }

    fn sent_attach(&self) -> bool {
        self.sent_attach
    }

    fn sent_host_input(&self) -> bool {
        false
    }

    fn sent_resize(&self) -> bool {
        self.sent
            .iter()
            .any(|m| matches!(m, ClientMessage::LegacyResize { .. }))
    }

    fn sent_mouse_report(&self) -> bool {
        false
    }

    fn sent_tiocswinsz(&self) -> bool {
        false
    }

    fn last_client_message(&self) -> Option<ClientMessage> {
        self.sent.last().cloned()
    }
}

/// Sole production implementation: local Unix-socket frame client.
pub struct UnixSocketFrameSource {
    token: String,
    sent_attach: bool,
}

impl UnixSocketFrameSource {
    pub fn from_gobby_home(home: &Path) -> anyhow::Result<Self> {
        Ok(Self {
            token: read_local_cli_token_for(home)?,
            sent_attach: false,
        })
    }

    pub fn from_env() -> anyhow::Result<Self> {
        Ok(Self {
            token: read_local_cli_token()?,
            sent_attach: false,
        })
    }
}

#[cfg(unix)]
impl UnixSocketFrameSource {
    fn handshake(
        &mut self,
        locator: &AttachLocator,
        cols: u16,
        rows: u16,
    ) -> Result<(std::os::unix::net::UnixStream, String), FrameError> {
        let mut stream =
            std::os::unix::net::UnixStream::connect(PathBuf::from(&locator.socket_path))?;
        let hello = ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: self.token.clone(),
            cols,
            rows,
        };
        write_message(&mut stream, &hello).map_err(|err| FrameError::Other(err.to_string()))?;
        let welcome = read_message::<_, ServerMessage>(&mut stream, MAX_FRAME_SIZE)
            .map_err(|err| FrameError::Other(err.to_string()))?;
        let ServerMessage::Welcome { host_epoch } = welcome else {
            return Err(FrameError::Other("expected welcome".into()));
        };
        if host_epoch != locator.frame_host_epoch {
            return Err(FrameError::HostEpochChanged {
                expected: locator.frame_host_epoch.clone(),
                actual: host_epoch,
            });
        }
        Ok((stream, host_epoch))
    }
}

#[cfg(unix)]
impl FrameSource for UnixSocketFrameSource {
    fn connect(
        &mut self,
        locator: &AttachLocator,
        cols: u16,
        rows: u16,
    ) -> Result<String, FrameError> {
        let (mut stream, epoch) = self.handshake(locator, cols, rows)?;
        let attach = ClientMessage::AttachTerminal {
            host_terminal_id: locator.host_terminal_id.clone(),
            reservation_id: None,
        };
        write_message(&mut stream, &attach).map_err(|err| FrameError::Other(err.to_string()))?;
        self.sent_attach = true;
        Ok(epoch)
    }

    fn send(&mut self, _message: &ClientMessage) -> Result<(), FrameError> {
        Err(FrameError::Io(io::Error::new(
            ErrorKind::NotConnected,
            "unix frame send requires a live session",
        )))
    }

    fn sent_attach(&self) -> bool {
        self.sent_attach
    }

    fn sent_host_input(&self) -> bool {
        false
    }

    fn sent_resize(&self) -> bool {
        false
    }

    fn sent_mouse_report(&self) -> bool {
        false
    }

    fn sent_tiocswinsz(&self) -> bool {
        false
    }

    fn last_client_message(&self) -> Option<ClientMessage> {
        None
    }
}
