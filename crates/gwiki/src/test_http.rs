#![allow(dead_code)]

use std::io::{self, ErrorKind, Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

pub(crate) type RequestHandle = thread::JoinHandle<io::Result<String>>;

pub(crate) fn spawn_json_response(body: impl Into<String>) -> io::Result<(String, RequestHandle)> {
    let body = body.into();
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let api_base = format!("http://{}", listener.local_addr()?);
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept()?;
        stream.set_read_timeout(Some(Duration::from_secs(2)))?;
        let request = read_http_request(&mut stream)?;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
            body.len()
        )?;
        Ok(request)
    });
    Ok((api_base, handle))
}

fn read_http_request(stream: &mut impl Read) -> io::Result<String> {
    let mut request = Vec::new();
    let mut chunk = [0_u8; 1024];
    loop {
        let read = match stream.read(&mut chunk) {
            Ok(read) => read,
            Err(error) if matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                break;
            }
            Err(error) => return Err(error),
        };
        if read == 0 {
            break;
        }
        request.extend_from_slice(&chunk[..read]);

        if let Some(header_end) = find_header_end(&request) {
            let header = String::from_utf8_lossy(&request[..header_end]);
            let Some(content_length) = content_length(&header) else {
                break;
            };
            let body_len = request.len().saturating_sub(header_end + 4);
            if body_len >= content_length {
                break;
            }
        }
    }
    String::from_utf8(request).map_err(|error| io::Error::new(ErrorKind::InvalidData, error))
}

fn find_header_end(request: &[u8]) -> Option<usize> {
    request.windows(4).position(|window| window == b"\r\n\r\n")
}

fn content_length(header: &str) -> Option<usize> {
    header.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.trim()
            .eq_ignore_ascii_case("content-length")
            .then(|| value.trim().parse().ok())
            .flatten()
    })
}
