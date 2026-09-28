//! Shared helpers for the front-door integration tests.

#![allow(dead_code)] // Each test binary uses a different subset.

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::time::Duration;

use gobby_daemon::serve::{PublicListener, serve};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;

pub const TIMEOUT: Duration = Duration::from_secs(10);

/// Start a front door on an ephemeral port that proxies to `backend`.
pub async fn start_front_door(backend: SocketAddr) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind front door");
    let addr = listener.local_addr().expect("front door addr");
    tokio::spawn(async move {
        let routes = BTreeMap::new();
        serve(
            vec![PublicListener { listener, backend }],
            &routes,
            std::future::pending(),
        )
        .await
        .expect("front door serve");
    });
    addr
}

/// An address nothing listens on.
pub async fn refused_addr() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().expect("addr");
    drop(listener);
    addr
}

/// What a raw WS backend saw: its upgrade request head and every byte after it.
pub struct WsCapture {
    pub request_head: String,
    pub received: Vec<u8>,
}

/// A raw WebSocket backend for one connection. It answers the upgrade with 101 and
/// `permessage-deflate`, reads exactly `expect_len` bytes, writes `reply`, then closes.
pub async fn ws_backend(
    expect_len: usize,
    reply: Vec<u8>,
) -> (SocketAddr, oneshot::Receiver<WsCapture>) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind ws backend");
    let addr = listener.local_addr().expect("ws backend addr");
    let (done, captured) = oneshot::channel();
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let (request_head, mut received) = read_head(&mut stream).await;
        stream
            .write_all(
                b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\nconnection: Upgrade\r\n\
                  sec-websocket-accept: stub\r\nsec-websocket-extensions: permessage-deflate\r\n\r\n",
            )
            .await
            .expect("write 101");
        while received.len() < expect_len {
            let mut chunk = [0_u8; 4096];
            let read = stream.read(&mut chunk).await.expect("read frames");
            assert_ne!(read, 0, "client leg closed early");
            received.extend_from_slice(&chunk[..read]);
        }
        stream.write_all(&reply).await.expect("write reply");
        stream.shutdown().await.expect("shutdown");
        let _ = done.send(WsCapture {
            request_head,
            received,
        });
    });
    (addr, captured)
}

/// Open a raw WS upgrade through `front_door`; returns the stream, the response head,
/// and any frame bytes that arrived with it.
pub async fn ws_client(front_door: SocketAddr, path: &str) -> (TcpStream, String, Vec<u8>) {
    let mut stream = TcpStream::connect(front_door)
        .await
        .expect("connect front door");
    let request = format!(
        "GET {path} HTTP/1.1\r\nhost: {front_door}\r\nupgrade: websocket\r\nconnection: Upgrade\r\n\
         sec-websocket-key: AAAAAAAAAAAAAAAAAAAAAA==\r\nsec-websocket-version: 13\r\n\
         sec-websocket-extensions: permessage-deflate\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .await
        .expect("write upgrade");
    let (head, rest) = read_head(&mut stream).await;
    (stream, head, rest)
}

/// Read until `len` bytes are buffered in `buffer`.
pub async fn read_exact_into(stream: &mut TcpStream, buffer: &mut Vec<u8>, len: usize) {
    while buffer.len() < len {
        let mut chunk = [0_u8; 4096];
        let read = stream.read(&mut chunk).await.expect("read");
        assert_ne!(
            read,
            0,
            "stream closed after {} of {len} bytes",
            buffer.len()
        );
        buffer.extend_from_slice(&chunk[..read]);
    }
}

/// Read an HTTP head; returns it and any bytes read past the blank line.
async fn read_head(stream: &mut TcpStream) -> (String, Vec<u8>) {
    let mut buffer = Vec::new();
    loop {
        if let Some(end) = buffer.windows(4).position(|window| window == b"\r\n\r\n") {
            let rest = buffer.split_off(end + 4);
            return (String::from_utf8(buffer).expect("utf8 head"), rest);
        }
        let mut chunk = [0_u8; 1024];
        let read = stream.read(&mut chunk).await.expect("read head");
        assert_ne!(read, 0, "closed before the end of the head");
        buffer.extend_from_slice(&chunk[..read]);
    }
}

/// Encode one WebSocket frame (RFC 6455). Client frames are masked.
pub fn frame(first_byte: u8, payload: &[u8], masked: bool) -> Vec<u8> {
    let mut out = vec![first_byte];
    let mask_bit = if masked { 0x80 } else { 0 };
    match payload.len() {
        len @ 0..=125 => out.push(mask_bit | len as u8),
        len @ 126..=0xFFFF => {
            out.push(mask_bit | 126);
            out.extend_from_slice(&(len as u16).to_be_bytes());
        }
        len => {
            out.push(mask_bit | 127);
            out.extend_from_slice(&(len as u64).to_be_bytes());
        }
    }
    if masked {
        let key = [0x37, 0xfa, 0x21, 0x3d];
        out.extend_from_slice(&key);
        out.extend(
            payload
                .iter()
                .enumerate()
                .map(|(i, byte)| byte ^ key[i % 4]),
        );
    } else {
        out.extend_from_slice(payload);
    }
    out
}

/// Lower-cased head, for header assertions.
pub fn head_has(head: &str, line: &str) -> bool {
    head.to_ascii_lowercase()
        .lines()
        .any(|candidate| candidate.trim() == line)
}
