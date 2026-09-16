//! Read-only frame protocol on `gterm-frames.sock`.

use std::io::{self, Cursor};
use std::os::fd::AsRawFd;
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

use super::backpressure::FrameMailbox;
use super::embed::{self, AttachOutcome};
use super::state::HostState;
use crate::protocol::{
    check_client_version, read_message, validate_dimensions, write_message, ClientMessage,
    FramingError, ServerMessage, TmuxClientIdentity, VersionCheck, MAX_FRAME_SIZE,
};

const PEER_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);

/// How much longer than `lag_timeout` a single frame write may block. The
/// broadcast pass declares an observer lagged at `lag_timeout` and queues the
/// `lagged` error for it; cancelling the in-flight write at the same instant
/// would race that pass, truncate the frame mid-message, and close the peer
/// without ever telling it why. The cancellation stays as the backstop for a
/// peer that never drains at all.
const LAG_CLOSE_GRACE: Duration = Duration::from_secs(1);

/// Cap what the kernel will hold for this socket's send side, which is what
/// bounds the bytes in flight to the peer.
pub(crate) fn set_send_buffer(stream: &UnixStream, bytes: u32) {
    let size = i32::try_from(bytes.max(256)).unwrap_or(i32::MAX);
    // SAFETY: `stream` is a live Unix socket; `SO_SNDBUF` takes an `i32` whose
    // storage outlives the call. Failure is ignored so a kernel that rejects the
    // size still accepts the connection.
    let _ = unsafe {
        libc::setsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_SNDBUF,
            (&size as *const i32).cast(),
            std::mem::size_of::<i32>() as libc::socklen_t,
        )
    };
}

pub async fn handle_connection(stream: UnixStream, state: Arc<HostState>) {
    set_send_buffer(&stream, state.config.delta_queue_bytes);
    let (reader, mut writer) = stream.into_split();
    let mut reader = BufReader::new(reader);
    let mut read_buffer = Vec::new();
    let hello = match read_frame::<ClientMessage>(&mut reader, &mut read_buffer).await {
        Ok(msg) => msg,
        Err(error) => {
            tracing::debug!(%error, "frame connection closed while reading hello");
            return;
        }
    };
    let ClientMessage::Hello {
        version,
        encoding: enc,
        local_token,
        cols,
        rows,
        tmux_identity,
    } = hello
    else {
        let _ = write_frame(
            &mut writer,
            &ServerMessage::Error {
                code: "hello_required".into(),
                message: None,
            },
        )
        .await;
        return;
    };
    if local_token != state.local_token {
        let _ = write_frame(
            &mut writer,
            &ServerMessage::Error {
                code: "invalid_token".into(),
                message: None,
            },
        )
        .await;
        return;
    }
    match check_client_version(version) {
        VersionCheck::Compatible => {}
        VersionCheck::Incompatible(reason) => {
            let _ = write_frame(
                &mut writer,
                &ServerMessage::Error {
                    code: "unsupported_protocol".into(),
                    message: Some(reason),
                },
            )
            .await;
            return;
        }
    }
    if let Err(err) = validate_dimensions(i64::from(rows), i64::from(cols)) {
        let _ = write_frame(
            &mut writer,
            &ServerMessage::Error {
                code: err.code().into(),
                message: None,
            },
        )
        .await;
        return;
    }
    let encoding = enc;
    let client_identity: Option<TmuxClientIdentity> = tmux_identity;
    let mut viewport = (rows, cols);
    if write_frame(
        &mut writer,
        &ServerMessage::Welcome {
            host_epoch: state.host_epoch.clone(),
        },
    )
    .await
    .is_err()
    {
        return;
    }

    let mut attachment_id: Option<u64> = None;
    let mut out_rx: Option<FrameMailbox> = None;
    loop {
        tokio::select! {
            incoming = read_frame::<ClientMessage>(&mut reader, &mut read_buffer) => {
                let msg = match incoming {
                    Ok(msg) => msg,
                    Err(error) => {
                        tracing::debug!(%error, "frame connection read failed");
                        drain_ready(&mut writer, &mut out_rx).await;
                        break;
                    }
                };
                if msg.is_legacy_unknown() {
                    let _ = write_frame(
                        &mut writer,
                        &ServerMessage::Error {
                            code: "unknown_message".into(),
                            message: Some("legacy write verb rejected".into()),
                        },
                    )
                    .await;
                    continue;
                }
                match msg {
                    ClientMessage::AttachTerminal {
                        host_terminal_id,
                        reservation_id,
                        locator,
                    } => {
                        match embed::attach_frame(
                            &state,
                            &host_terminal_id,
                            reservation_id,
                            locator,
                            client_identity.clone(),
                            encoding,
                            viewport.0,
                            viewport.1,
                        )
                        .await
                        {
                            Ok(AttachOutcome {
                                attachment_id: id,
                                host_terminal_id: assigned,
                                created,
                                rx,
                            }) => {
                                attachment_id = Some(id);
                                out_rx = Some(rx);
                                let _ = write_frame(
                                    &mut writer,
                                    &ServerMessage::Attached {
                                        created,
                                        host_terminal_id: assigned,
                                    },
                                )
                                .await;
                            }
                            Err(code) => {
                                let _ = write_frame(
                                    &mut writer,
                                    &ServerMessage::Error {
                                        code: code.into(),
                                        message: None,
                                    },
                                )
                                .await;
                            }
                        }
                    }
                    ClientMessage::SetViewport { rows, cols } => {
                        if let Some(id) = attachment_id {
                            if let Err(code) = state.set_viewport(id, rows, cols).await {
                                let _ = write_frame(
                                    &mut writer,
                                    &ServerMessage::Error {
                                        code: code.into(),
                                        message: None,
                                    },
                                )
                                .await;
                            } else {
                                viewport = (rows, cols);
                            }
                        }
                    }
                    ClientMessage::SetScrollOffset { rows_from_live_edge } => {
                        if let Some(id) = attachment_id {
                            match state.set_scroll(id, rows_from_live_edge).await {
                                Ok(applied) => {
                                    let _ = write_frame(&mut writer, &applied).await;
                                }
                                Err(code) => {
                                    let _ = write_frame(
                                        &mut writer,
                                        &ServerMessage::Error {
                                            code: code.into(),
                                            message: None,
                                        },
                                    )
                                    .await;
                                }
                            }
                        }
                    }
                    ClientMessage::Detach => {
                        if let Some(id) = attachment_id.take() {
                            embed::detach_frame(&state, id).await;
                        }
                        out_rx = None;
                    }
                    ClientMessage::Hello { .. }
                    | ClientMessage::LegacyInput { .. }
                    | ClientMessage::LegacyClipboard { .. }
                    | ClientMessage::LegacyResize { .. } => {}
                }
            }
            outgoing = recv_opt(&mut out_rx) => {
                let Some(msg) = outgoing else {
                    tracing::debug!("frame connection sender closed");
                    break;
                };
                match tokio::time::timeout(
                    state.config.lag_timeout() + LAG_CLOSE_GRACE,
                    write_frame(&mut writer, &msg),
                )
                .await
                {
                    Ok(Ok(())) => {
                        if let Some(mailbox) = out_rx.as_ref() {
                            mailbox.note_drain();
                        }
                    }
                    Ok(Err(error)) => {
                        tracing::debug!(%error, "frame connection write failed");
                        break;
                    }
                    Err(_) => {
                        if let Some(mailbox) = out_rx.as_ref() {
                            mailbox.close_with(
                                ServerMessage::Error {
                                    code: "lagged".into(),
                                    message: None,
                                },
                                state.config.delta_queue_bytes as usize,
                            );
                        }
                        break;
                    }
                }
            }
        }
    }
    if let Some(id) = attachment_id {
        embed::detach_frame(&state, id).await;
    }
    if let Err(error) = writer.shutdown().await {
        tracing::debug!(%error, "frame connection write shutdown failed");
    }
    // Keep the socket alive until the peer consumes the final frames; dropping both halves can
    // discard unread bytes even after the write half has shut down.
    let mut sink = [0_u8; 256];
    let _ = tokio::time::timeout(PEER_DRAIN_TIMEOUT, async {
        loop {
            match reader.read(&mut sink).await {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
        }
    })
    .await;
}

async fn recv_opt(rx: &mut Option<FrameMailbox>) -> Option<ServerMessage> {
    match rx.as_mut() {
        Some(rx) => rx.recv().await,
        None => std::future::pending().await,
    }
}

async fn drain_ready(writer: &mut tokio::net::unix::OwnedWriteHalf, rx: &mut Option<FrameMailbox>) {
    let Some(rx) = rx.as_mut() else {
        return;
    };
    while let Some(message) = rx.try_pop() {
        if let Err(error) = write_frame(writer, &message).await {
            tracing::debug!(%error, "frame connection drain failed");
            break;
        }
        rx.note_drain();
    }
}

async fn fill_frame_buffer(
    reader: &mut (impl AsyncBufRead + Unpin),
    buffer: &mut Vec<u8>,
    target_len: usize,
) -> io::Result<()> {
    while buffer.len() < target_len {
        let available = reader.fill_buf().await?;
        if available.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "frame connection closed mid-message",
            ));
        }
        let take = available.len().min(target_len - buffer.len());
        buffer.extend_from_slice(&available[..take]);
        reader.consume(take);
    }
    Ok(())
}

async fn read_frame<M: for<'de> serde::Deserialize<'de>>(
    reader: &mut (impl AsyncBufRead + Unpin),
    buffer: &mut Vec<u8>,
) -> io::Result<M> {
    fill_frame_buffer(reader, buffer, 4).await?;
    let len_buf: [u8; 4] = buffer[..4]
        .try_into()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid frame prefix"))?;
    let len = u32::from_le_bytes(len_buf) as usize;
    if len > MAX_FRAME_SIZE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            FramingError::Oversized {
                claimed: len,
                max: MAX_FRAME_SIZE,
            },
        ));
    }
    let frame_len = 4 + len;
    fill_frame_buffer(reader, buffer, frame_len).await?;
    let framed = buffer.drain(..frame_len).collect::<Vec<_>>();
    read_message(&mut Cursor::new(framed), MAX_FRAME_SIZE)
        .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))
}

async fn write_frame<M: serde::Serialize>(
    writer: &mut tokio::net::unix::OwnedWriteHalf,
    msg: &M,
) -> io::Result<()> {
    let mut buf = Vec::new();
    write_message(&mut buf, msg).map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))?;
    writer.write_all(&buf).await?;
    writer.flush().await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn cancelled_frame_read_preserves_partial_prefix() {
        let (mut writer, reader) = tokio::io::duplex(1024);
        let mut reader = BufReader::new(reader);
        let mut buffer = Vec::new();
        let mut framed = Vec::new();
        write_message(&mut framed, &ClientMessage::Detach).expect("encode frame");

        writer
            .write_all(&framed[..2])
            .await
            .expect("write partial prefix");
        assert!(tokio::time::timeout(
            Duration::from_millis(10),
            read_frame::<ClientMessage>(&mut reader, &mut buffer),
        )
        .await
        .is_err());

        writer
            .write_all(&framed[2..])
            .await
            .expect("write remaining frame");
        assert!(matches!(
            read_frame::<ClientMessage>(&mut reader, &mut buffer)
                .await
                .expect("decode frame after cancellation"),
            ClientMessage::Detach
        ));
    }
}
