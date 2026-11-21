//! The worker channel: the parent's end of one worker connection, the length-prefixed frames that
//! ride it, and the per-spawn credential that the worker reads from its own inherited pipe.
//!
//! A Rust-mode spawn gets a connected socket pair and a credential pipe. The child inherits only
//! the child end of the socket and the read end of the pipe, each named by its variable. The
//! parent end is close-on-exec, and the connection has no listener, path or port, so it is
//! private to this one child. The transport is unix only. Other targets return an unsupported
//! error from `attach`, and the crate still builds there.

use std::fmt;
use std::io;

use super::worker_handshake::{ChannelError, WorkerChannel, WorkerSession};

#[cfg(unix)]
use std::io::Write as _;
#[cfg(unix)]
use std::os::fd::{AsRawFd, OwnedFd, RawFd};
#[cfg(unix)]
use std::os::unix::net::UnixStream;

#[cfg(unix)]
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
#[cfg(unix)]
use rand::RngCore;
#[cfg(unix)]
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};

/// Set only by gdaemon, and only in Rust mode, to mark a start that serves the worker channel.
pub const WORKER_MARKER_ENV: &str = "GOBBY_WORKER_MARKER";
/// Names the descriptor of the child end of the worker connection.
pub const WORKER_CHANNEL_FD_ENV: &str = "GOBBY_WORKER_CHANNEL_FD";
/// Names the descriptor that carries the credential of this spawn.
pub const WORKER_CREDENTIAL_FD_ENV: &str = "GOBBY_WORKER_CREDENTIAL_FD";
/// Width of the big-endian length prefix in front of every frame.
pub const FRAME_PREFIX_BYTES: usize = 4;

/// The parent's end of one worker connection.
#[cfg(unix)]
pub struct WorkerLink {
    stream: tokio::net::UnixStream,
}

/// Off unix there is no worker connection, so the link is empty.
#[cfg(not(unix))]
pub struct WorkerLink;

#[cfg(unix)]
impl AsRawFd for WorkerLink {
    fn as_raw_fd(&self) -> RawFd {
        self.stream.as_raw_fd()
    }
}

#[cfg(unix)]
impl WorkerChannel for WorkerLink {
    async fn exchange(
        &mut self,
        request: &[u8],
        reply_limit: usize,
    ) -> Result<Vec<u8>, ChannelError> {
        let prefix = u32::try_from(request.len()).map_err(|_| ChannelError)?;
        self.stream
            .write_all(&prefix.to_be_bytes())
            .await
            .map_err(|_| ChannelError)?;
        self.stream
            .write_all(request)
            .await
            .map_err(|_| ChannelError)?;
        read_frame(&mut self.stream, reply_limit).await
    }
}

#[cfg(not(unix))]
impl WorkerChannel for WorkerLink {
    async fn exchange(
        &mut self,
        _request: &[u8],
        _reply_limit: usize,
    ) -> Result<Vec<u8>, ChannelError> {
        Err(ChannelError)
    }
}

/// The parent's side of one worker: its link, its session and the credential it was given. The
/// credential is never printed.
pub struct WorkerAttachment {
    pub link: WorkerLink,
    pub session: WorkerSession,
    pub credential: String,
}

impl fmt::Debug for WorkerAttachment {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("WorkerAttachment")
            .field("session", &self.session)
            .finish_non_exhaustive()
    }
}

/// What one spawn hands over: the parent's attachment, and the descriptors the child inherits.
/// The parent holds the child descriptors only until the spawn returns.
#[derive(Debug)]
#[cfg_attr(not(unix), allow(dead_code))]
pub(crate) struct WorkerSpawn {
    pub attachment: WorkerAttachment,
    #[cfg(unix)]
    pub child_fds: Vec<OwnedFd>,
    #[cfg(unix)]
    pub channel_fd: RawFd,
    #[cfg(unix)]
    pub credential_fd: RawFd,
}

/// Creates the worker connection and the credential pipe for one spawn. The session is the one the
/// spawn drew, so the attachment serves that generation. The parent end is registered with the
/// Tokio reactor, so this must run inside the runtime.
#[cfg(unix)]
pub(crate) fn attach(session: WorkerSession) -> io::Result<WorkerSpawn> {
    let (parent, child) = UnixStream::pair()?;
    parent.set_nonblocking(true)?;
    let stream = tokio::net::UnixStream::from_std(parent)?;
    let mut secret = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut secret);
    let credential = URL_SAFE_NO_PAD.encode(secret);
    let (credential_reader, mut credential_writer) = std::io::pipe()?;
    credential_writer.write_all(credential.as_bytes())?;
    drop(credential_writer);
    let channel_fd = child.as_raw_fd();
    let credential_fd = credential_reader.as_raw_fd();
    Ok(WorkerSpawn {
        attachment: WorkerAttachment {
            link: WorkerLink { stream },
            session,
            credential,
        },
        child_fds: vec![OwnedFd::from(child), OwnedFd::from(credential_reader)],
        channel_fd,
        credential_fd,
    })
}

/// Off unix no worker channel exists, so no spawn can attach one. Its only caller, the
/// backend supervisor, is unix-only.
#[cfg(not(unix))]
#[allow(dead_code)]
pub(crate) fn attach(_session: WorkerSession) -> io::Result<WorkerSpawn> {
    Err(io::Error::from(io::ErrorKind::Unsupported))
}

/// Reads one frame: a big-endian length prefix, then the body. A prefix over `limit` is refused
/// before the body is allocated or read. An end of stream or any read error is a channel error.
#[cfg(unix)]
pub(crate) async fn read_frame<R: AsyncRead + Unpin>(
    reader: &mut R,
    limit: usize,
) -> Result<Vec<u8>, ChannelError> {
    let mut prefix = [0u8; FRAME_PREFIX_BYTES];
    reader
        .read_exact(&mut prefix)
        .await
        .map_err(|_| ChannelError)?;
    let length = usize::try_from(u32::from_be_bytes(prefix)).map_err(|_| ChannelError)?;
    if length > limit {
        return Err(ChannelError);
    }
    let mut body = vec![0u8; length];
    reader
        .read_exact(&mut body)
        .await
        .map_err(|_| ChannelError)?;
    Ok(body)
}

#[cfg(all(test, unix))]
#[path = "worker_channel/tests.rs"]
mod tests;
