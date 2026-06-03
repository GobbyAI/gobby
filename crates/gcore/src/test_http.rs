use std::io::{self, ErrorKind, Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

pub(crate) type RequestHandle = thread::JoinHandle<io::Result<String>>;

pub(crate) fn spawn_json_response(body: impl Into<String>) -> io::Result<(String, RequestHandle)> {
    spawn_response(200, "OK", "application/json", body.into())
}

pub(crate) fn spawn_json_response_with_status(
    status: u16,
    body: impl Into<String>,
) -> io::Result<(String, RequestHandle)> {
    spawn_response(
        status,
        reason_phrase(status),
        "application/json",
        body.into(),
    )
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        204 => "No Content",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        422 => "Unprocessable Entity",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "",
    }
}

pub(crate) fn spawn_response(
    status: u16,
    reason: &'static str,
    content_type: &'static str,
    body: String,
) -> io::Result<(String, RequestHandle)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let api_base = format!("http://{}", listener.local_addr()?);
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept()?;
        stream.set_read_timeout(Some(Duration::from_secs(2)))?;
        let request = read_http_request(&mut stream)?;
        write!(
            stream,
            "HTTP/1.1 {status} {reason}\r\ncontent-type: {content_type}\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
            body.len()
        )?;
        Ok(request)
    });
    Ok((api_base, handle))
}

pub(crate) fn read_http_request(stream: &mut impl Read) -> io::Result<String> {
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

#[cfg(test)]
mod tests {
    use super::{content_length, read_http_request};
    use std::io::{self, ErrorKind, Read};

    #[test]
    fn content_length_parses_case_and_whitespace_variants() {
        assert_eq!(content_length("CONTENT-LENGTH: 12\r\n"), Some(12));
        assert_eq!(content_length("content-length :\t7\r\n"), Some(7));
        assert_eq!(content_length("content-type: text/plain\r\n"), None);
    }

    #[test]
    fn header_only_request_without_content_length_is_complete() {
        struct SingleRead {
            data: &'static [u8],
        }

        impl Read for SingleRead {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                if buffer.is_empty() {
                    return Ok(0);
                }
                if self.data.is_empty() {
                    return Err(io::Error::new(
                        ErrorKind::Other,
                        "reader should not be polled after complete headers",
                    ));
                }
                let read = buffer.len().min(self.data.len());
                buffer[..read].copy_from_slice(&self.data[..read]);
                self.data = &self.data[read..];
                Ok(read)
            }
        }

        let mut reader = SingleRead {
            data: b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n",
        };

        let request = read_http_request(&mut reader).expect("header-only request");

        assert_eq!(request, "GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n");
    }
}
