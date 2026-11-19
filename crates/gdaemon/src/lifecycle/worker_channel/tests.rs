//! Tests for the worker channel: the parent end of the connection, the frame reader, and the
//! pinned byte limits, deadlines and environment names.

use std::io;
use std::os::fd::AsRawFd;
use std::time::Duration;

use tokio::io::AsyncWriteExt;

use super::{
    FRAME_PREFIX_BYTES, WORKER_CHANNEL_FD_ENV, WORKER_CREDENTIAL_FD_ENV, WORKER_MARKER_ENV, attach,
    read_frame,
};
use crate::lifecycle::worker_handshake::{
    COMMAND_REPLY_DEADLINE, ChannelError, HANDSHAKE_REPLY_DEADLINE, MAX_DOMAIN_OP_REPLY_BYTES,
    MAX_WORKER_REPLY_BYTES, WorkerSession,
};

/// The parent's end of the connection must not reach the child: only the child end is passed.
#[tokio::test]
async fn parent_end_is_close_on_exec() {
    let session = WorkerSession::new(1).expect("generation 1 is nonzero");
    let attached = attach(session);
    assert!(
        attached.is_ok(),
        "attach must create the worker connection: {attached:?}"
    );
    let spawn = attached.expect("attach succeeded");
    let parent = spawn.attachment.link.as_raw_fd();
    // SAFETY: fcntl reads the descriptor flags of the parent end, which the attachment owns.
    let flags = unsafe { libc::fcntl(parent, libc::F_GETFD) };
    assert!(
        flags >= 0,
        "the parent end is open: {}",
        io::Error::last_os_error()
    );
    assert_ne!(
        flags & libc::FD_CLOEXEC,
        0,
        "the parent end must be close-on-exec"
    );
}

/// A frame whose prefix is over the limit is refused from the prefix alone, and a frame at the
/// limit is read whole. The writers stay open, so a refusal cannot be an end of stream.
#[tokio::test]
async fn reply_frame_over_its_limit_is_refused_before_allocation() {
    let (mut over_writer, mut over_reader) = tokio::io::duplex(1 << 17);
    over_writer
        .write_all(&65_537u32.to_be_bytes())
        .await
        .expect("write the over-limit prefix");
    assert_eq!(
        read_frame(&mut over_reader, 65_536).await,
        Err(ChannelError),
        "a prefix over the limit is refused"
    );

    let body = vec![b'x'; 65_536];
    let (mut at_writer, mut at_reader) = tokio::io::duplex(1 << 17);
    at_writer
        .write_all(&65_536u32.to_be_bytes())
        .await
        .expect("write the at-limit prefix");
    at_writer
        .write_all(&body)
        .await
        .expect("write the at-limit body");
    let read = read_frame(&mut at_reader, 65_536).await;
    assert_eq!(
        read.as_ref().map(Vec::len),
        Ok(65_536),
        "a frame at the limit is read whole"
    );
    assert!(
        read.is_ok_and(|frame| frame == body),
        "the body arrives unchanged"
    );
}

/// The byte limits, deadlines, frame prefix and environment names are pinned. A change to any of
/// them fails here.
#[test]
fn byte_limits_and_deadlines_are_pinned() {
    assert_eq!(MAX_WORKER_REPLY_BYTES, 65_536);
    assert_eq!(MAX_DOMAIN_OP_REPLY_BYTES, 16 * 1024 * 1024);
    assert_eq!(HANDSHAKE_REPLY_DEADLINE, Duration::from_secs(60));
    assert_eq!(COMMAND_REPLY_DEADLINE, Duration::from_secs(300));
    assert_eq!(FRAME_PREFIX_BYTES, 4);
    assert_eq!(WORKER_MARKER_ENV, "GOBBY_WORKER_MARKER");
    assert_eq!(WORKER_CHANNEL_FD_ENV, "GOBBY_WORKER_CHANNEL_FD");
    assert_eq!(WORKER_CREDENTIAL_FD_ENV, "GOBBY_WORKER_CREDENTIAL_FD");
}
