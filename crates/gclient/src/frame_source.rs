//! Ordered terminal-frame transports used by workspace panes.

pub mod proxy;

pub use proxy::ProxyFrameSource;

use std::collections::VecDeque;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use gobby_core::local_token::{read_local_cli_token, read_local_cli_token_for};
use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, FramingError, RenderEncoding,
    ServerMessage, MAX_FRAME_SIZE, PROTOCOL_VERSION,
};
use thiserror::Error;
use tokio::io::AsyncWriteExt;
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::UnixStream;
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinHandle;
use tokio::time::timeout;

const DIRECT_FRAME_CAPACITY: usize = 256;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttachLocator {
    pub backend: String,
    pub frame_host_epoch: String,
    pub host_terminal_id: String,
    pub socket_path: String,
    pub pane_id: Option<String>,
    pub server_pid: Option<i32>,
    pub server_start_time: Option<i64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Transport {
    Direct,
    Proxy,
}

#[derive(Debug, Error)]
pub enum FrameError {
    #[error("frame host epoch changed from {expected} to {actual}")]
    HostEpochChanged { expected: String, actual: String },
    #[error("frame source reached end of stream")]
    Eof,
    #[error("frame source lagged")]
    Lag,
    #[error("frame source operation was cancelled")]
    Cancelled,
    #[error("attachment was finalized ({code}): {reason}")]
    Finalized { code: String, reason: String },
    #[error("frame I/O failed: {0}")]
    Io(String),
    #[error("frame protocol failed: {0}")]
    Protocol(String),
    #[error("frame source failed: {0}")]
    Other(String),
}

impl From<std::io::Error> for FrameError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error.to_string())
    }
}

impl From<FramingError> for FrameError {
    fn from(error: FramingError) -> Self {
        match error {
            FramingError::Eof | FramingError::UnexpectedEof => Self::Eof,
            other => Self::Protocol(other.to_string()),
        }
    }
}

impl From<crate::daemon::DaemonError> for FrameError {
    fn from(error: crate::daemon::DaemonError) -> Self {
        Self::Protocol(error.to_string())
    }
}

#[allow(async_fn_in_trait)]
pub trait FrameSource {
    async fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError>;
    async fn recv(&mut self) -> Result<ServerMessage, FrameError>;
    fn transport(&self) -> Transport;
}

#[derive(Debug)]
pub struct ScriptedFrameSource {
    transport: Transport,
    welcome_epoch: String,
    sent: Vec<ClientMessage>,
    inbound: VecDeque<ServerMessage>,
}

impl ScriptedFrameSource {
    pub fn new(transport: Transport) -> Self {
        Self {
            transport,
            welcome_epoch: "epoch".into(),
            sent: Vec::new(),
            inbound: VecDeque::new(),
        }
    }

    pub fn set_welcome_epoch(&mut self, epoch: impl Into<String>) {
        self.welcome_epoch = epoch.into();
    }

    pub fn connect(
        &mut self,
        locator: &AttachLocator,
        _cols: u16,
        _rows: u16,
    ) -> Result<String, FrameError> {
        if self.welcome_epoch != locator.frame_host_epoch {
            return Err(FrameError::HostEpochChanged {
                expected: locator.frame_host_epoch.clone(),
                actual: self.welcome_epoch.clone(),
            });
        }
        self.sent.push(crate::views::observe_tmux_pane(locator).1);
        Ok(self.welcome_epoch.clone())
    }

    pub fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError> {
        self.sent.push(message.clone());
        Ok(())
    }

    pub fn queue(&mut self, message: ServerMessage) {
        self.inbound.push_back(message);
    }

    pub fn sent_attach(&self) -> bool {
        self.sent
            .iter()
            .any(|message| matches!(message, ClientMessage::AttachTerminal { .. }))
    }

    pub fn sent_host_input(&self) -> bool {
        false
    }

    pub fn sent_resize(&self) -> bool {
        false
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
    async fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError> {
        ScriptedFrameSource::send(self, message)
    }

    async fn recv(&mut self) -> Result<ServerMessage, FrameError> {
        self.inbound.pop_front().ok_or(FrameError::Eof)
    }

    fn transport(&self) -> Transport {
        self.transport
    }
}

#[derive(Debug)]
pub enum PaneFrameSource {
    Direct(UnixSocketFrameSource),
    Proxy(ProxyFrameSource),
    Scripted(ScriptedFrameSource),
}

impl PaneFrameSource {
    pub fn scripted(&self) -> Option<&ScriptedFrameSource> {
        match self {
            Self::Scripted(source) => Some(source),
            Self::Direct(_) | Self::Proxy(_) => None,
        }
    }

    pub fn scripted_mut(&mut self) -> Option<&mut ScriptedFrameSource> {
        match self {
            Self::Scripted(source) => Some(source),
            Self::Direct(_) | Self::Proxy(_) => None,
        }
    }
}

impl FrameSource for PaneFrameSource {
    async fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError> {
        match self {
            Self::Direct(source) => FrameSource::send(source, message).await,
            Self::Proxy(source) => FrameSource::send(source, message).await,
            Self::Scripted(source) => FrameSource::send(source, message).await,
        }
    }

    async fn recv(&mut self) -> Result<ServerMessage, FrameError> {
        match self {
            Self::Direct(source) => source.recv().await,
            Self::Proxy(source) => source.recv().await,
            Self::Scripted(source) => source.recv().await,
        }
    }

    fn transport(&self) -> Transport {
        match self {
            Self::Direct(source) => source.transport(),
            Self::Proxy(source) => source.transport(),
            Self::Scripted(source) => FrameSource::transport(source),
        }
    }
}

#[derive(Debug, Clone)]
enum RetireReason {
    Eof,
    Lag,
    Cancelled,
    Io(String),
    Protocol(String),
}

impl RetireReason {
    fn into_error(self) -> FrameError {
        match self {
            Self::Eof => FrameError::Eof,
            Self::Lag => FrameError::Lag,
            Self::Cancelled => FrameError::Cancelled,
            Self::Io(detail) => FrameError::Io(detail),
            Self::Protocol(detail) => FrameError::Protocol(detail),
        }
    }
}

type Retired = Arc<Mutex<Option<RetireReason>>>;

struct WriteRequest {
    message: ClientMessage,
    done: oneshot::Sender<Result<(), FrameError>>,
}

pub struct UnixSocketFrameSource {
    outbound: mpsc::Sender<WriteRequest>,
    inbound: mpsc::Receiver<ServerMessage>,
    retired: Retired,
    shutdown: watch::Sender<bool>,
    _cleanup: JoinHandle<()>,
}

impl std::fmt::Debug for UnixSocketFrameSource {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("UnixSocketFrameSource")
            .field("retired", &self.retired_reason())
            .finish_non_exhaustive()
    }
}

impl UnixSocketFrameSource {
    pub async fn connect(
        locator: &AttachLocator,
        local_token: impl Into<String>,
        cols: u16,
        rows: u16,
    ) -> Result<Self, FrameError> {
        let stream = timeout(CONNECT_TIMEOUT, UnixStream::connect(&locator.socket_path))
            .await
            .map_err(|_| FrameError::Io("frame socket connect timed out".into()))??;
        Self::connect_stream(stream, locator, local_token, cols, rows).await
    }

    #[doc(hidden)]
    pub async fn connect_stream(
        mut stream: UnixStream,
        locator: &AttachLocator,
        local_token: impl Into<String>,
        cols: u16,
        rows: u16,
    ) -> Result<Self, FrameError> {
        let hello = ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: local_token.into(),
            cols,
            rows,
            tmux_identity: crate::tmux_identity::current(),
        };
        timeout(CONNECT_TIMEOUT, write_message_async(&mut stream, &hello))
            .await
            .map_err(|_| FrameError::Io("frame hello timed out".into()))??;
        let welcome: ServerMessage = timeout(
            CONNECT_TIMEOUT,
            read_message_async(&mut stream, MAX_FRAME_SIZE),
        )
        .await
        .map_err(|_| FrameError::Io("frame welcome timed out".into()))??;
        let actual = match welcome {
            ServerMessage::Welcome { host_epoch } => host_epoch,
            other => {
                return Err(FrameError::Protocol(format!(
                    "expected Welcome, received {other:?}"
                )))
            }
        };
        if actual != locator.frame_host_epoch {
            return Err(FrameError::HostEpochChanged {
                expected: locator.frame_host_epoch.clone(),
                actual,
            });
        }
        let attach = crate::views::observe_tmux_pane(locator).1;
        timeout(CONNECT_TIMEOUT, write_message_async(&mut stream, &attach))
            .await
            .map_err(|_| FrameError::Io("frame attach timed out".into()))??;
        Ok(Self::from_stream(stream))
    }

    pub async fn from_gobby_home(
        home: &Path,
        locator: &AttachLocator,
        cols: u16,
        rows: u16,
    ) -> Result<Self, FrameError> {
        let home = home.to_path_buf();
        let token = timeout(
            CONNECT_TIMEOUT,
            tokio::task::spawn_blocking(move || read_local_cli_token_for(&home)),
        )
        .await
        .map_err(|_| FrameError::Io("local token read timed out".into()))?
        .map_err(|error| FrameError::Io(error.to_string()))?
        .map_err(|error| FrameError::Io(error.to_string()))?;
        Self::connect(locator, token.trim(), cols, rows).await
    }

    pub async fn from_env(
        locator: &AttachLocator,
        cols: u16,
        rows: u16,
    ) -> Result<Self, FrameError> {
        let token = timeout(
            CONNECT_TIMEOUT,
            tokio::task::spawn_blocking(read_local_cli_token),
        )
        .await
        .map_err(|_| FrameError::Io("local token read timed out".into()))?
        .map_err(|error| FrameError::Io(error.to_string()))?
        .map_err(|error| FrameError::Io(error.to_string()))?;
        Self::connect(locator, token.trim(), cols, rows).await
    }

    fn from_stream(stream: UnixStream) -> Self {
        let (read_half, write_half) = stream.into_split();
        let (frame_tx, inbound) = mpsc::channel(DIRECT_FRAME_CAPACITY);
        let (write_tx, write_rx) = mpsc::channel(16);
        let retired = Arc::new(Mutex::new(None));
        let (shutdown, _) = watch::channel(false);
        let reader_shutdown = shutdown.subscribe();
        let writer_shutdown = shutdown.subscribe();
        let reader = tokio::spawn(run_reader(
            read_half,
            frame_tx,
            Arc::clone(&retired),
            shutdown.clone(),
            reader_shutdown,
        ));
        let writer = tokio::spawn(run_writer(
            write_half,
            write_rx,
            Arc::clone(&retired),
            shutdown.clone(),
            writer_shutdown,
        ));
        let cleanup = tokio::spawn(shutdown_socket(reader, writer));

        Self {
            outbound: write_tx,
            inbound,
            retired,
            shutdown,
            _cleanup: cleanup,
        }
    }

    fn retired_reason(&self) -> Option<RetireReason> {
        self.retired
            .lock()
            .expect("frame retirement mutex poisoned")
            .clone()
    }

    fn ensure_active(&self) -> Result<(), FrameError> {
        self.retired_reason()
            .map_or(Ok(()), |reason| Err(reason.into_error()))
    }

    fn retire_cancelled(&mut self) {
        set_retired(&self.retired, RetireReason::Cancelled);
        let _ = self.shutdown.send(true);
    }

    #[doc(hidden)]
    pub fn cancel_reader_task(&mut self) {
        self.retire_cancelled();
    }

    #[doc(hidden)]
    pub fn cancel_writer_task(&mut self) {
        self.retire_cancelled();
    }
}

impl FrameSource for UnixSocketFrameSource {
    async fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError> {
        self.ensure_active()?;
        if !matches!(
            message,
            ClientMessage::SetViewport { .. }
                | ClientMessage::SetScrollOffset { .. }
                | ClientMessage::Detach
        ) {
            return Err(FrameError::Protocol(
                "message is not valid after frame attachment".into(),
            ));
        }

        let (done, result) = oneshot::channel();
        let mut guard = SendCancellationGuard::new(self);
        guard
            .source
            .outbound
            .send(WriteRequest {
                message: message.clone(),
                done,
            })
            .await
            .map_err(|_| {
                guard
                    .source
                    .retired_reason()
                    .unwrap_or(RetireReason::Eof)
                    .into_error()
            })?;
        let outcome = result.await.map_err(|_| {
            guard
                .source
                .retired_reason()
                .unwrap_or(RetireReason::Eof)
                .into_error()
        })?;
        guard.armed = false;
        outcome
    }

    async fn recv(&mut self) -> Result<ServerMessage, FrameError> {
        if let Ok(message) = self.inbound.try_recv() {
            return Ok(message);
        }
        if let Some(reason) = self.retired_reason() {
            return Err(reason.into_error());
        }
        self.inbound.recv().await.ok_or_else(|| {
            self.retired_reason()
                .unwrap_or(RetireReason::Eof)
                .into_error()
        })
    }

    fn transport(&self) -> Transport {
        Transport::Direct
    }
}

impl Drop for UnixSocketFrameSource {
    fn drop(&mut self) {
        self.retire_cancelled();
    }
}

struct SendCancellationGuard<'a> {
    source: &'a mut UnixSocketFrameSource,
    armed: bool,
}

impl<'a> SendCancellationGuard<'a> {
    fn new(source: &'a mut UnixSocketFrameSource) -> Self {
        Self {
            source,
            armed: true,
        }
    }
}

impl Drop for SendCancellationGuard<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.source.retire_cancelled();
        }
    }
}

async fn run_reader(
    mut reader: OwnedReadHalf,
    frames: mpsc::Sender<ServerMessage>,
    retired: Retired,
    shutdown: watch::Sender<bool>,
    mut shutdown_rx: watch::Receiver<bool>,
) -> OwnedReadHalf {
    loop {
        let read = tokio::select! {
            biased;
            _ = shutdown_rx.changed() => return reader,
            read = read_message_async(&mut reader, MAX_FRAME_SIZE) => read,
        };
        let message = match read {
            Ok(message) => message,
            Err(FramingError::Eof | FramingError::UnexpectedEof) => {
                set_retired(&retired, RetireReason::Eof);
                let _ = shutdown.send(true);
                return reader;
            }
            Err(FramingError::Io(error)) => {
                set_retired(&retired, RetireReason::Io(error.to_string()));
                let _ = shutdown.send(true);
                return reader;
            }
            Err(error) => {
                set_retired(&retired, RetireReason::Protocol(error.to_string()));
                let _ = shutdown.send(true);
                return reader;
            }
        };
        match frames.try_send(message) {
            Ok(()) => {}
            Err(mpsc::error::TrySendError::Full(_)) => {
                set_retired(&retired, RetireReason::Lag);
                let _ = shutdown.send(true);
                return reader;
            }
            Err(mpsc::error::TrySendError::Closed(_)) => return reader,
        }
    }
}

async fn run_writer(
    mut writer: OwnedWriteHalf,
    mut requests: mpsc::Receiver<WriteRequest>,
    retired: Retired,
    shutdown: watch::Sender<bool>,
    mut shutdown_rx: watch::Receiver<bool>,
) -> OwnedWriteHalf {
    loop {
        let request = tokio::select! {
            biased;
            _ = shutdown_rx.changed() => return writer,
            request = requests.recv() => match request {
                Some(request) => request,
                None => return writer,
            },
        };
        let outcome = tokio::select! {
            biased;
            _ = shutdown_rx.changed() => {
                let error = retired
                    .lock()
                    .expect("frame retirement mutex poisoned")
                    .clone()
                    .unwrap_or(RetireReason::Cancelled)
                    .into_error();
                let _ = request.done.send(Err(error));
                return writer;
            }
            outcome = timeout(
                CONNECT_TIMEOUT,
                write_message_async(&mut writer, &request.message),
            ) => outcome
                .map_err(|_| FrameError::Io("frame write timed out".into()))
                .and_then(|result| result.map_err(FrameError::from)),
        };
        match outcome {
            Ok(()) => {
                let _ = request.done.send(Ok(()));
            }
            Err(error) => {
                let reason = match &error {
                    FrameError::Io(detail) => RetireReason::Io(detail.clone()),
                    _ => RetireReason::Protocol(error.to_string()),
                };
                set_retired(&retired, reason);
                let _ = request.done.send(Err(error));
                let _ = shutdown.send(true);
                return writer;
            }
        }
    }
}

async fn shutdown_socket(reader: JoinHandle<OwnedReadHalf>, writer: JoinHandle<OwnedWriteHalf>) {
    let (reader, writer) = tokio::join!(reader, writer);
    let (Ok(reader), Ok(writer)) = (reader, writer) else {
        return;
    };
    if let Ok(mut stream) = reader.reunite(writer) {
        let _ = stream.shutdown().await;
    }
}

fn set_retired(retired: &Retired, reason: RetireReason) {
    let mut slot = retired.lock().expect("frame retirement mutex poisoned");
    if slot.is_none() {
        *slot = Some(reason);
    }
}
