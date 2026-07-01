#![cfg(test)]

use std::io::Read;

pub(crate) fn read_http_request(stream: &mut impl Read) -> String {
    let mut request = Vec::new();
    let mut chunk = [0_u8; 1024];
    let mut header_end = None;
    let mut content_length = None;

    loop {
        let n = stream.read(&mut chunk).unwrap();
        assert!(n > 0, "client closed before request body was fully read");
        request.extend_from_slice(&chunk[..n]);

        if header_end.is_none()
            && let Some(pos) = request.windows(4).position(|window| window == b"\r\n\r\n")
        {
            let end = pos + 4;
            header_end = Some(end);
            let headers = String::from_utf8_lossy(&request[..end]);
            content_length = Some(
                headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("Content-Length")
                            .then(|| value.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0),
            );
        }

        if let (Some(end), Some(len)) = (header_end, content_length)
            && request.len() >= end + len
        {
            return String::from_utf8(request).unwrap();
        }
    }
}
