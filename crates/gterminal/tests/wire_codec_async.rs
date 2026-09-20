use gobby_terminal::protocol::{
    read_message, read_message_async, write_message, write_message_async, CellData, ClientMessage,
    CursorState, FrameData, FramingError, PaneLocator, PaneModes, RenderEncoding, ServerMessage,
    TerminalFrame, TmuxClientIdentity, MAX_FRAME_SIZE, PROTOCOL_VERSION,
};
use serde::{Deserialize, Serialize};
use std::fmt::Debug;
use std::io::Cursor;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

#[tokio::test]
async fn async_framing_matches_sync_contract() {
    for message in client_messages() {
        assert_sync_async_contract(&message).await;
    }
    for message in server_messages() {
        assert_sync_async_contract(&message).await;
    }

    let sync_eof = read_message::<_, ServerMessage>(&mut Cursor::new(Vec::new()), MAX_FRAME_SIZE)
        .expect_err("clean sync EOF");
    let async_eof = read_message_async::<_, ServerMessage>(&mut &[][..], MAX_FRAME_SIZE)
        .await
        .expect_err("clean async EOF");
    assert!(matches!(sync_eof, FramingError::Eof));
    assert!(matches!(async_eof, FramingError::Eof));

    let truncated_prefix = [1_u8, 0];
    let sync_truncated =
        read_message::<_, ServerMessage>(&mut Cursor::new(truncated_prefix), MAX_FRAME_SIZE)
            .expect_err("truncated sync prefix");
    let async_truncated =
        read_message_async::<_, ServerMessage>(&mut &truncated_prefix[..], MAX_FRAME_SIZE)
            .await
            .expect_err("truncated async prefix");
    assert!(matches!(sync_truncated, FramingError::UnexpectedEof));
    assert!(matches!(async_truncated, FramingError::UnexpectedEof));

    let truncated_payload = [4_u8, 0, 0, 0, 1, 2];
    let sync_truncated =
        read_message::<_, ServerMessage>(&mut Cursor::new(truncated_payload), MAX_FRAME_SIZE)
            .expect_err("truncated sync payload");
    let async_truncated =
        read_message_async::<_, ServerMessage>(&mut &truncated_payload[..], MAX_FRAME_SIZE)
            .await
            .expect_err("truncated async payload");
    assert!(matches!(sync_truncated, FramingError::UnexpectedEof));
    assert!(matches!(async_truncated, FramingError::UnexpectedEof));

    let oversized = (MAX_FRAME_SIZE as u32 + 1).to_le_bytes();
    let sync_oversized =
        read_message::<_, ServerMessage>(&mut Cursor::new(oversized), MAX_FRAME_SIZE)
            .expect_err("sync oversize");
    let async_oversized =
        read_message_async::<_, ServerMessage>(&mut &oversized[..], MAX_FRAME_SIZE)
            .await
            .expect_err("async oversize");
    assert!(matches!(
        sync_oversized,
        FramingError::Oversized { claimed, max }
            if claimed == MAX_FRAME_SIZE + 1 && max == MAX_FRAME_SIZE
    ));
    assert!(matches!(
        async_oversized,
        FramingError::Oversized { claimed, max }
            if claimed == MAX_FRAME_SIZE + 1 && max == MAX_FRAME_SIZE
    ));
}

async fn assert_sync_async_contract<M>(message: &M)
where
    M: Serialize + for<'de> Deserialize<'de> + Debug + PartialEq,
{
    let mut sync_bytes = Vec::new();
    write_message(&mut sync_bytes, message).expect("sync encode");

    let mut async_writer = OneByteWriter::default();
    write_message_async(&mut async_writer, message)
        .await
        .expect("one-byte async encode");
    assert_eq!(async_writer.bytes, sync_bytes);

    let sync_decoded: M =
        read_message(&mut Cursor::new(&sync_bytes), MAX_FRAME_SIZE).expect("sync decode");
    let mut async_reader = OneByteReader::new(sync_bytes);
    let async_decoded: M = read_message_async(&mut async_reader, MAX_FRAME_SIZE)
        .await
        .expect("one-byte async decode");
    assert_eq!(&sync_decoded, message);
    assert_eq!(async_decoded, sync_decoded);
}

fn client_messages() -> Vec<ClientMessage> {
    vec![
        ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: "token".into(),
            cols: 80,
            rows: 24,
            tmux_identity: Some(TmuxClientIdentity {
                socket_path: "/tmp/tmux".into(),
                server_pid: 7,
                server_start_time: 9,
                pane_id: "%1".into(),
            }),
        },
        ClientMessage::LegacyInput { data: vec![1, 2] },
        ClientMessage::LegacyClipboard {
            extension: "png".into(),
            data: vec![3, 4],
        },
        ClientMessage::LegacyResize {
            cols: 100,
            rows: 40,
            cell_width_px: 8,
            cell_height_px: 16,
        },
        ClientMessage::Detach,
        ClientMessage::AttachTerminal {
            host_terminal_id: "host-1".into(),
            reservation_id: None,
            locator: Some(PaneLocator {
                socket_path: "/tmp/tmux".into(),
                server_pid: 7,
                server_start_time: 9,
                pane_id: "%1".into(),
            }),
        },
        ClientMessage::SetViewport {
            rows: 42,
            cols: 120,
        },
        ClientMessage::SetScrollOffset {
            rows_from_live_edge: 11,
        },
        ClientMessage::ReadText {
            start_rows_from_live_edge: 31,
            start_col: 2,
            end_rows_from_live_edge: 4,
            end_col: 17,
        },
    ]
}

fn server_messages() -> Vec<ServerMessage> {
    vec![
        ServerMessage::Welcome {
            host_epoch: "epoch-async".into(),
        },
        ServerMessage::Frame(FrameData {
            cells: vec![CellData {
                symbol: "λ".into(),
                fg: 2,
                bg: 3,
                modifier: 1,
                skip: false,
                hyperlink: Some(0),
            }],
            width: 1,
            height: 1,
            cursor: Some(CursorState {
                x: 0,
                y: 0,
                visible: true,
                shape: 1,
            }),
            hyperlinks: vec!["https://example.test".into()],
            graphics: vec![5, 6],
            modes: PaneModes::default(),
        }),
        ServerMessage::Terminal(TerminalFrame {
            seq: 4,
            width: 80,
            height: 24,
            full: false,
            bytes: b"delta".to_vec(),
        }),
        ServerMessage::Graphics { bytes: vec![7, 8] },
        ServerMessage::AttachHistory {
            text: "history".into(),
            truncated: true,
            dropped_bytes: 2,
            total_bytes: 9,
        },
        ServerMessage::ScrollOffsetApplied {
            applied_rows: 3,
            max_rows: 30,
        },
        ServerMessage::TerminalExited {
            host_terminal_id: "host-1".into(),
            exit_code: Some(17),
        },
        ServerMessage::Error {
            code: "refused".into(),
            message: Some("inside target pane".into()),
        },
        ServerMessage::Attached {
            created: true,
            host_terminal_id: "host-1".into(),
        },
        ServerMessage::TextRead {
            text: "retained text".into(),
            truncated: false,
        },
    ]
}

#[tokio::test]
async fn async_writer_cancellation_retires_connection() {
    let message = ServerMessage::Welcome {
        host_epoch: "epoch-cancel".into(),
    };

    for write_limit in [2, 6] {
        let bytes = Arc::new(Mutex::new(Vec::new()));
        let writer = StallingWriter::new(write_limit, bytes.clone());
        let task_message = message.clone();
        let task = tokio::spawn(async move {
            let mut writer = writer;
            write_message_async(&mut writer, &task_message).await
        });
        tokio::task::yield_now().await;
        task.abort();
        assert!(task.await.expect_err("cancelled writer").is_cancelled());

        let partial = bytes.lock().expect("bytes lock").clone();
        assert_eq!(partial.len(), write_limit);
        assert!(
            read_message::<_, ServerMessage>(&mut Cursor::new(partial), MAX_FRAME_SIZE).is_err()
        );

        let mut fresh = Vec::new();
        write_message_async(&mut fresh, &message)
            .await
            .expect("fresh writer remains usable");
        let decoded: ServerMessage = read_message_async(&mut fresh.as_slice(), MAX_FRAME_SIZE)
            .await
            .expect("fresh reader");
        assert_eq!(decoded, message);
    }
}

#[derive(Default)]
struct OneByteWriter {
    bytes: Vec<u8>,
}

impl AsyncWrite for OneByteWriter {
    fn poll_write(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        let Some(byte) = buf.first() else {
            return Poll::Ready(Ok(0));
        };
        self.bytes.push(*byte);
        Poll::Ready(Ok(1))
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

struct OneByteReader {
    bytes: Vec<u8>,
    offset: usize,
}

impl OneByteReader {
    fn new(bytes: Vec<u8>) -> Self {
        Self { bytes, offset: 0 }
    }
}

impl AsyncRead for OneByteReader {
    fn poll_read(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        if let Some(byte) = self.bytes.get(self.offset).copied() {
            buf.put_slice(&[byte]);
            self.offset += 1;
        }
        Poll::Ready(Ok(()))
    }
}

struct StallingWriter {
    remaining: usize,
    bytes: Arc<Mutex<Vec<u8>>>,
}

impl StallingWriter {
    fn new(remaining: usize, bytes: Arc<Mutex<Vec<u8>>>) -> Self {
        Self { remaining, bytes }
    }
}

impl AsyncWrite for StallingWriter {
    fn poll_write(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        if self.remaining == 0 {
            return Poll::Pending;
        }
        let written = self.remaining.min(buf.len());
        self.bytes
            .lock()
            .expect("bytes lock")
            .extend_from_slice(&buf[..written]);
        self.remaining -= written;
        Poll::Ready(Ok(written))
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}
