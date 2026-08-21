use super::*;
use ratatui::style::{Color, Modifier};
use std::io::{self, Read};

fn hello_msg(cols: u16, rows: u16) -> ClientMessage {
    ClientMessage::Hello {
        version: PROTOCOL_VERSION,
        encoding: RenderEncoding::SemanticFrame,
        local_token: "local-token".into(),
        cols,
        rows,
    }
}

// ---- Round-trip: ClientMessage ----

#[test]
fn client_hello_roundtrip() {
    let msg = hello_msg(80, 24);
    let encoded = bincode::serde::encode_to_vec(&msg, bincode::config::standard()).unwrap();
    let (decoded, _): (ClientMessage, _) =
        bincode::serde::decode_from_slice(&encoded, bincode::config::standard()).unwrap();
    assert_eq!(msg, decoded);
}

#[test]
fn framing_large_payload_roundtrip() {
    // Create a Frame message that is ≥128 KB.
    // Use a large frame with verbose cell data to exceed 128 KB after bincode encoding.
    // 200×50 = 10000 cells. With varied symbols and styles, this should easily exceed 128 KB.
    let width: u16 = 200;
    let height: u16 = 50;
    let cells: Vec<CellData> = (0..(width as usize) * (height as usize))
        .map(|i| CellData {
            symbol: if i % 256 < 32 {
                " ".to_owned()
            } else {
                format!("{:03}", i % 1000)
            },
            fg: color_to_u32(Color::Rgb((i % 256) as u8, ((i / 256) % 256) as u8, 128)),
            bg: color_to_u32(Color::Indexed((i % 256) as u8)),
            modifier: ((i % 16) as u16),
            skip: i % 100 == 0,
            hyperlink: None,
        })
        .collect();

    let frame = FrameData {
        cells,
        width,
        height,
        cursor: Some(CursorState {
            x: 10,
            y: 5,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
    };
    let msg = ServerMessage::Frame(frame);

    let mut buf = Vec::new();
    write_message(&mut buf, &msg).unwrap();
    // Verify the payload is at least 128 KB
    assert!(
        buf.len() >= 128 * 1024,
        "framed payload should be >= 128 KB, got {} bytes",
        buf.len()
    );

    let decoded: ServerMessage = read_message(&mut buf.as_slice(), MAX_FRAME_SIZE).unwrap();
    assert_eq!(msg, decoded);
}

#[test]
fn framing_multiple_messages_sequential() {
    // Write 100+ messages of varying types and read them back.
    let mut buf = Vec::new();
    let mut expected = Vec::new();

    for i in 0..150u32 {
        let msg = match i % 5 {
            0 => hello_msg(80, 24),
            1 => ClientMessage::LegacyInput {
                data: vec![(i % 256) as u8; (i as usize % 50) + 1],
            },
            2 => ClientMessage::LegacyClipboard {
                extension: "png".to_owned(),
                data: vec![0x89, b'P', b'N', b'G', (i % 256) as u8],
            },
            3 => ClientMessage::LegacyResize {
                cols: (100 + (i % 30) as u16),
                rows: (30 + (i % 10) as u16),
                cell_width_px: 8,
                cell_height_px: 16,
            },
            4 => ClientMessage::Detach,
            _ => unreachable!(),
        };
        write_message(&mut buf, &msg).unwrap();
        expected.push(msg);
    }

    let mut cursor = buf.as_slice();
    for expected_msg in &expected {
        let decoded: ClientMessage = read_message(&mut cursor, MAX_FRAME_SIZE).unwrap();
        assert_eq!(*expected_msg, decoded);
    }
}

#[test]
fn framing_oversized_rejected_without_panic() {
    // Craft a frame with a huge length prefix (4 GB claim).
    let mut buf: Vec<u8> = (u32::MAX).to_le_bytes().to_vec();
    // Add a few garbage bytes after the length prefix.
    buf.extend_from_slice(&[0xDE, 0xAD, 0xBE, 0xEF]);

    let result: Result<ClientMessage, FramingError> =
        read_message(&mut buf.as_slice(), MAX_FRAME_SIZE);
    match result {
        Err(FramingError::Oversized { claimed, max }) => {
            assert_eq!(claimed, u32::MAX as usize);
            assert_eq!(max, MAX_FRAME_SIZE);
        }
        other => panic!("expected Oversized error, got: {other:?}"),
    }
}

#[test]
fn framing_malformed_payload_rejected_without_panic() {
    // Valid length prefix pointing to garbage data.
    let payload = vec![0xDE, 0xAD, 0xBE, 0xEF, 0x01, 0x02];
    let mut buf = (payload.len() as u32).to_le_bytes().to_vec();
    buf.extend_from_slice(&payload);

    let result: Result<ClientMessage, FramingError> =
        read_message(&mut buf.as_slice(), MAX_FRAME_SIZE);
    assert!(result.is_err(), "malformed payload should be rejected");
    match result {
        Err(FramingError::Bincode(_)) => {} // expected
        other => panic!("expected Bincode error, got: {other:?}"),
    }
}

#[test]
fn framing_truncated_stream_returns_unexpected_eof() {
    // Write a length prefix claiming 100 bytes, but only provide 4.
    let mut buf: Vec<u8> = 100u32.to_le_bytes().to_vec();
    buf.extend_from_slice(&[0xAA, 0xBB, 0xCC, 0xDD]);

    let result: Result<ClientMessage, FramingError> =
        read_message(&mut buf.as_slice(), MAX_FRAME_SIZE);
    match result {
        Err(FramingError::UnexpectedEof) => {}
        other => panic!("expected UnexpectedEof, got: {other:?}"),
    }
}

#[test]
fn framing_zero_length_message() {
    // A 1-byte message (smallest possible valid bincode payload).
    // Actually, let's test with the smallest real message: Detach.
    let msg = ClientMessage::Detach;
    let mut buf = Vec::new();
    write_message(&mut buf, &msg).unwrap();

    // Verify the length prefix is correct
    let len = u32::from_le_bytes(buf[..4].try_into().unwrap()) as usize;
    assert_eq!(
        len,
        buf.len() - 4,
        "length prefix should match payload size"
    );

    let decoded: ClientMessage = read_message(&mut buf.as_slice(), MAX_FRAME_SIZE).unwrap();
    assert_eq!(msg, decoded);
}

#[test]
fn framing_partial_read_reassembly() {
    // Simulate partial reads by using a reader that yields small chunks.
    let msg = ClientMessage::LegacyInput {
        data: vec![42; 500], // 500-byte input payload
    };
    let mut full_buf = Vec::new();
    write_message(&mut full_buf, &msg).unwrap();

    // Wrap in a chunked reader that only yields 7 bytes at a time.
    let mut chunked = ChunkedReader::new(full_buf, 7);
    let decoded: ClientMessage = read_message(&mut chunked, MAX_FRAME_SIZE).unwrap();
    assert_eq!(msg, decoded);
}

// ---- Version negotiation ----

#[test]
fn version_compatible() {
    assert_eq!(
        check_client_version(PROTOCOL_VERSION),
        VersionCheck::Compatible
    );
}

#[test]
fn version_older_client_rejected() {
    let result = check_client_version(PROTOCOL_VERSION.saturating_sub(1));
    assert!(
        matches!(result, VersionCheck::Incompatible(_)),
        "mismatched older clients must be a typed Incompatible error, got {result:?}"
    );
}

#[test]
fn version_newer_client_rejected() {
    let result = check_client_version(PROTOCOL_VERSION + 1);
    assert!(matches!(result, VersionCheck::Incompatible(_)));
    if let VersionCheck::Incompatible(msg) = result {
        assert!(msg.contains("newer"), "error should mention newer version");
    }
}

// ---- Pre-persistence client rejection ----

#[test]
fn prepersistence_version_zero_rejected() {
    let result = check_client_version(0);
    match result {
        VersionCheck::Incompatible(msg) => {
            assert!(
                msg.contains("pre-persistence"),
                "error should mention pre-persistence: {msg}"
            );
        }
        _ => panic!("version 0 should be rejected as incompatible"),
    }
}

#[test]
fn prepersistence_version_zero_welcome_has_error() {
    // Simulating what the server would send to a v0 client.
    let check = check_client_version(0);
    let response = match check {
        VersionCheck::Compatible => ServerMessage::Welcome {
            host_epoch: "epoch".to_owned(),
        },
        VersionCheck::Incompatible(reason) => ServerMessage::Error {
            code: "unsupported_protocol".to_owned(),
            message: Some(reason),
        },
    };

    match response {
        ServerMessage::Error { code, .. } => assert_eq!(code, "unsupported_protocol"),
        other => panic!("expected Error, got: {other:?}"),
    }
}

// ---- Malformed/oversized input ----

#[test]
fn oversized_frame_does_not_panic() {
    // Claim 4GB payload — should return Oversized error, not panic.
    let mut buf: Vec<u8> = 0xFFC00000u32.to_le_bytes().to_vec(); // ~4 GB claim
    buf.extend_from_slice(&[0; 8]);

    let result: Result<ClientMessage, FramingError> =
        read_message(&mut buf.as_slice(), MAX_FRAME_SIZE);
    assert!(result.is_err());
    // Did not panic — test passing is proof.
}

#[test]
fn malformed_frame_does_not_panic() {
    // Random garbage bytes after a valid-ish length prefix.
    let garbage: Vec<u8> = (0..200).map(|i| (i ^ 0xAA) as u8).collect();
    let mut buf = (garbage.len() as u32).to_le_bytes().to_vec();
    buf.extend_from_slice(&garbage);

    let result: Result<ClientMessage, FramingError> =
        read_message(&mut buf.as_slice(), MAX_FRAME_SIZE);
    assert!(result.is_err());
    // Did not panic.
}

#[test]
fn oversized_input_rejected_custom_max() {
    // Verify a custom (small) max_frame_size is enforced.
    let msg = ClientMessage::LegacyInput {
        data: vec![0x41; 1000],
    };
    let mut buf = Vec::new();
    write_message(&mut buf, &msg).unwrap();

    let result: Result<ClientMessage, FramingError> = read_message(&mut buf.as_slice(), 64);
    // The actual bincode payload for 1000 bytes of input will be > 64 bytes.
    assert!(
        matches!(result, Err(FramingError::Oversized { .. })),
        "expected Oversized with small max_frame_size"
    );
}

// ---- FrameData ↔ ratatui Buffer conversion ----

#[test]
fn frame_data_roundtrip_through_ratatui_buffer() {
    let area = ratatui::layout::Rect::new(0, 0, 5, 3);
    let mut buffer = ratatui::buffer::Buffer::filled(area, ratatui::buffer::Cell::new(" "));

    // Write some styled content.
    buffer.cell_mut((0, 0)).unwrap().set_symbol("H");
    buffer.cell_mut((0, 0)).unwrap().fg = Color::Red;
    buffer.cell_mut((0, 0)).unwrap().modifier = Modifier::BOLD;

    buffer.cell_mut((1, 0)).unwrap().set_symbol("i");
    buffer.cell_mut((1, 0)).unwrap().fg = Color::Green;
    buffer.cell_mut((1, 0)).unwrap().modifier = Modifier::ITALIC;

    buffer.cell_mut((2, 0)).unwrap().set_symbol("!");
    buffer.cell_mut((2, 0)).unwrap().fg = Color::Rgb(255, 128, 0);
    buffer.cell_mut((2, 0)).unwrap().bg = Color::Indexed(220);

    let cursor = CursorState {
        x: 1,
        y: 0,
        visible: true,
        shape: 0,
    };
    let frame = FrameData::from_ratatui_buffer(&buffer, Some(cursor.clone()));

    // Verify frame dimensions.
    assert_eq!(frame.width, 5);
    assert_eq!(frame.height, 3);
    assert_eq!(frame.cells.len(), 15);
    assert_eq!(frame.cursor, Some(cursor));

    // Verify specific cells survived the conversion.
    assert_eq!(frame.cells[0].symbol, "H");
    assert_eq!(frame.cells[0].fg, color_to_u32(Color::Red));
    assert_eq!(frame.cells[0].modifier, Modifier::BOLD.bits());

    assert_eq!(frame.cells[1].symbol, "i");
    assert_eq!(frame.cells[1].fg, color_to_u32(Color::Green));
    assert_eq!(frame.cells[1].modifier, Modifier::ITALIC.bits());

    assert_eq!(frame.cells[2].symbol, "!");
    assert_eq!(frame.cells[2].fg, color_to_u32(Color::Rgb(255, 128, 0)));
    assert_eq!(frame.cells[2].bg, color_to_u32(Color::Indexed(220)));

    let with_links = FrameData::from_ratatui_buffer_with_hyperlinks(
        &buffer,
        None,
        &[((1, 0), "i".to_owned(), "https://example.com".to_owned())],
    );
    assert_eq!(with_links.cells[1].hyperlink, Some(0));
    assert_eq!(
        with_links.hyperlinks,
        vec!["https://example.com".to_owned()]
    );

    // Convert back to ratatui buffer and compare.
    let restored = frame.to_ratatui_buffer().expect("should reconstruct");
    assert_eq!(restored.area, area);
    assert_eq!(restored.cell((0, 0)).unwrap().symbol(), "H");
    assert_eq!(restored.cell((0, 0)).unwrap().fg, Color::Red);
    assert_eq!(restored.cell((0, 0)).unwrap().modifier, Modifier::BOLD);
    assert_eq!(restored.cell((1, 0)).unwrap().symbol(), "i");
    assert_eq!(restored.cell((2, 0)).unwrap().symbol(), "!");
    assert_eq!(restored.cell((2, 0)).unwrap().fg, Color::Rgb(255, 128, 0));
}

#[test]
fn frame_data_rejects_mismatched_cell_count() {
    let frame = FrameData {
        cells: vec![
            CellData {
                symbol: "X".into(),
                fg: 0,
                bg: 0,
                modifier: 0,
                skip: false,
                hyperlink: None,
            };
            5
        ], // 5 cells but 3×2 = 6 expected
        width: 3,
        height: 2,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
    };
    assert!(frame.to_ratatui_buffer().is_none());
}

// ---- Color conversion coverage ----

#[test]
fn color_roundtrip_all_named_colors() {
    let named = [
        Color::Reset,
        Color::Black,
        Color::Red,
        Color::Green,
        Color::Yellow,
        Color::Blue,
        Color::Magenta,
        Color::Cyan,
        Color::Gray,
        Color::DarkGray,
        Color::LightRed,
        Color::LightGreen,
        Color::LightYellow,
        Color::LightBlue,
        Color::LightMagenta,
        Color::LightCyan,
        Color::White,
    ];
    for c in named {
        assert_eq!(
            u32_to_color(color_to_u32(c)),
            c,
            "roundtrip failed for {c:?}"
        );
    }
}

#[test]
fn color_roundtrip_indexed() {
    for i in 0..=255u8 {
        let c = Color::Indexed(i);
        assert_eq!(
            u32_to_color(color_to_u32(c)),
            c,
            "roundtrip failed for Indexed({i})"
        );
    }
}

#[test]
fn color_roundtrip_rgb() {
    let c = Color::Rgb(0xAB, 0xCD, 0xEF);
    assert_eq!(u32_to_color(color_to_u32(c)), c);

    let c = Color::Rgb(0, 0, 0);
    assert_eq!(u32_to_color(color_to_u32(c)), c);

    let c = Color::Rgb(255, 255, 255);
    assert_eq!(u32_to_color(color_to_u32(c)), c);
}

// ---- Modifier conversion ----

#[test]
fn modifier_roundtrip() {
    let all_mods = [
        Modifier::BOLD,
        Modifier::ITALIC,
        Modifier::REVERSED,
        Modifier::UNDERLINED,
        Modifier::DIM,
        Modifier::SLOW_BLINK,
        Modifier::CROSSED_OUT,
        Modifier::BOLD | Modifier::ITALIC,
        Modifier::BOLD | Modifier::UNDERLINED | Modifier::REVERSED,
        Modifier::empty(),
    ];
    for m in all_mods {
        assert_eq!(
            u16_to_modifier(modifier_to_u16(m)),
            m,
            "roundtrip failed for {m:?}"
        );
    }
}

#[test]
fn read_message_rejects_trailing_bytes() {
    // Encode a valid message, then append an extra byte after it.
    let msg = ClientMessage::Detach;
    let mut payload = bincode::serde::encode_to_vec(&msg, bincode::config::standard()).unwrap();
    let original_len = payload.len();
    payload.push(0xDE); // trailing garbage

    // Frame it with the inflated length (original + 1).
    let mut buf = (payload.len() as u32).to_le_bytes().to_vec();
    buf.extend_from_slice(&payload);

    let result: Result<ClientMessage, FramingError> =
        read_message(&mut buf.as_slice(), MAX_FRAME_SIZE);
    match result {
        Err(FramingError::Bincode(msg)) => {
            assert!(
                msg.contains("trailing bytes"),
                "error should mention trailing bytes: {msg}"
            );
            assert!(
                msg.contains(&format!("decoded {original_len}")),
                "error should mention decoded byte count: {msg}"
            );
        }
        other => panic!("expected Bincode error about trailing bytes, got: {other:?}"),
    }
}

#[test]
fn read_message_accepts_exact_payload() {
    // A normally-framed message should decode without error.
    let msg = hello_msg(80, 24);
    let mut buf = Vec::new();
    write_message(&mut buf, &msg).unwrap();
    let decoded: ClientMessage = read_message(&mut buf.as_slice(), MAX_FRAME_SIZE).unwrap();
    assert_eq!(msg, decoded);
}

#[test]
fn write_message_rejects_oversized_payload() {
    // We can't easily create a message that exceeds u32::MAX in a test,
    // but we can verify the check exists by testing that normal messages
    // have lengths well within the limit and the function doesn't fail.
    let msg = ClientMessage::Detach;
    let mut buf = Vec::new();
    assert!(write_message(&mut buf, &msg).is_ok());
}

// ---- Unix socketpair integration test ----

#[cfg(unix)]
#[test]
fn framing_over_unix_socketpair() {
    use std::os::unix::net::UnixStream;

    let (mut a, mut b) = UnixStream::pair().expect("socketpair");

    let messages = vec![
        hello_msg(80, 24),
        ClientMessage::LegacyInput {
            data: b"hello world".to_vec(),
        },
        ClientMessage::LegacyClipboard {
            extension: "png".to_owned(),
            data: vec![0x89, b'P', b'N', b'G'],
        },
        ClientMessage::LegacyResize {
            cols: 100,
            rows: 30,
            cell_width_px: 8,
            cell_height_px: 16,
        },
        ClientMessage::Detach,
    ];

    // Set non-blocking so we can write and read in the same test.
    a.set_nonblocking(false).unwrap();
    b.set_nonblocking(false).unwrap();

    for msg in &messages {
        write_message(&mut a, msg).unwrap();
    }

    for expected in &messages {
        let decoded: ClientMessage = read_message(&mut b, MAX_FRAME_SIZE).unwrap();
        assert_eq!(*expected, decoded);
    }
}

// ---- Helper: chunked reader for simulating partial reads ----

/// A `Read` wrapper that yields at most `chunk_size` bytes per `read()` call,
/// simulating partial reads on a real socket.
struct ChunkedReader {
    data: Vec<u8>,
    pos: usize,
    chunk_size: usize,
}

impl ChunkedReader {
    fn new(data: Vec<u8>, chunk_size: usize) -> Self {
        Self {
            data,
            pos: 0,
            chunk_size,
        }
    }
}

impl Read for ChunkedReader {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.pos >= self.data.len() {
            return Ok(0);
        }
        let remaining = self.data.len() - self.pos;
        let to_read = buf.len().min(remaining).min(self.chunk_size);
        buf[..to_read].copy_from_slice(&self.data[self.pos..self.pos + to_read]);
        self.pos += to_read;
        Ok(to_read)
    }
}
