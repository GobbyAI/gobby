//! The worker channel: the parent's end of one worker connection, the framing that rides it, and
//! the per-spawn credential that the worker reads from its own inherited pipe.
//!
//! The operations here answer with the unimplemented result until the transport is wired.

use std::fmt;
use std::io;
use std::os::fd::{AsRawFd, OwnedFd, RawFd};

use tokio::io::AsyncRead;

use super::worker_handshake::{ChannelError, GenerationSource, WorkerChannel, WorkerSession};

/// Set only by gdaemon, and only in Rust mode, to mark a start that serves the worker channel.
pub const WORKER_MARKER_ENV: &str = "GOBBY_WORKER_MARKER";
/// Names the descriptor of the child end of the worker connection.
pub const WORKER_CHANNEL_FD_ENV: &str = "GOBBY_WORKER_CHANNEL_FD";
/// Names the descriptor that carries the credential of this spawn.
pub const WORKER_CREDENTIAL_FD_ENV: &str = "GOBBY_WORKER_CREDENTIAL_FD";
/// Width of the big-endian length prefix in front of every frame.
pub const FRAME_PREFIX_BYTES: usize = 4;

/// The parent's end of one worker connection. Until the transport is wired it has no descriptor,
/// and every exchange is a channel error.
pub struct WorkerLink;

impl AsRawFd for WorkerLink {
    fn as_raw_fd(&self) -> RawFd {
        -1
    }
}

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

/// What one spawn hands over: the parent's attachment and the two descriptors the child inherits.
#[derive(Debug)]
pub(crate) struct WorkerSpawn {
    pub attachment: WorkerAttachment,
    pub child_fds: Vec<OwnedFd>,
    pub channel_fd: RawFd,
    pub credential_fd: RawFd,
}

/// Creates the worker connection and the credential pipe for one spawn.
pub(crate) fn attach(_generations: &GenerationSource) -> io::Result<WorkerSpawn> {
    Err(io::Error::other("worker channel not implemented"))
}

/// Reads one frame: a big-endian length prefix, then the body. A prefix over `limit` is refused
/// before the body is read.
pub(crate) async fn read_frame<R: AsyncRead + Unpin>(
    _reader: &mut R,
    _limit: usize,
) -> Result<Vec<u8>, ChannelError> {
    Err(ChannelError)
}

#[cfg(all(test, unix))]
#[path = "worker_channel/tests.rs"]
mod tests;
