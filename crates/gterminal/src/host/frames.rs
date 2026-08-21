//! Read-only frame protocol on `gterm-frames.sock`.

use std::io::{self, Cursor};
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

use super::state::HostState;
use crate::protocol::{
    check_client_version, read_message, validate_dimensions, write_message, ClientMessage,
    FramingError, RenderEncoding, ServerMessage, VersionCheck, MAX_FRAME_SIZE, PROTOCOL_VERSION,
};

pub async fn handle_connection(stream: UnixStream, state: Arc<HostState>) {
    let (mut reader, mut writer) = stream.into_split();
    let hello = match read_frame::<ClientMessage>(&mut reader).await {
        Ok(msg) => msg,
        Err(_) => return,
    };
    let ClientMessage::Hello {
        version,
        encoding: enc,
        local_token,
        cols,
        rows,
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
    let mut out_rx: Option<tokio::sync::mpsc::Receiver<ServerMessage>> = None;
    loop {
        tokio::select! {
            incoming = read_frame::<ClientMessage>(&mut reader) => {
                let msg = match incoming {
                    Ok(msg) => msg,
                    Err(_) => break,
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
                    } => {
                        match state
                            .attach(&host_terminal_id, reservation_id, encoding, viewport.0, viewport.1)
                            .await
                        {
                            Ok((id, rx)) => {
                                attachment_id = Some(id);
                                out_rx = Some(rx);
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
                            state.detach(id).await;
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
                if let Some(msg) = outgoing {
                    if write_frame(&mut writer, &msg).await.is_err() {
                        break;
                    }
                }
            }
        }
    }
    if let Some(id) = attachment_id {
        state.detach(id).await;
    }
}

async fn recv_opt(
    rx: &mut Option<tokio::sync::mpsc::Receiver<ServerMessage>>,
) -> Option<ServerMessage> {
    match rx.as_mut() {
        Some(rx) => rx.recv().await,
        None => std::future::pending().await,
    }
}

async fn read_frame<M: for<'de> serde::Deserialize<'de>>(
    reader: &mut tokio::net::unix::OwnedReadHalf,
) -> io::Result<M> {
    let mut len_buf = [0u8; 4];
    reader.read_exact(&mut len_buf).await?;
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
    let mut payload = vec![0u8; len];
    reader.read_exact(&mut payload).await?;
    let mut framed = Vec::with_capacity(4 + len);
    framed.extend_from_slice(&len_buf);
    framed.extend_from_slice(&payload);
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

#[allow(dead_code)]
fn _protocol_version() -> u32 {
    PROTOCOL_VERSION
}

#[allow(dead_code)]
fn _duration() -> Duration {
    Duration::from_millis(1)
}
