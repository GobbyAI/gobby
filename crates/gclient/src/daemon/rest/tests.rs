use super::*;
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::Notify;

async fn read_json_response(response: &'static [u8]) -> Result<Value, DaemonError> {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move {
        tokio::time::timeout(REQUEST_DEADLINE, async move {
            let (mut stream, _) = listener.accept().await.expect("accept");
            let mut request = [0_u8; 4096];
            let mut received = 0;
            while !request[..received]
                .windows(4)
                .any(|boundary| boundary == b"\r\n\r\n")
            {
                assert!(
                    received < request.len(),
                    "request headers exceed fixture buffer"
                );
                let count = stream
                    .read(&mut request[received..])
                    .await
                    .expect("read request");
                assert_ne!(count, 0, "request closed before headers completed");
                received += count;
            }
            stream.write_all(response).await.expect("write response");
            stream.shutdown().await.expect("end response");
        })
        .await
        .expect("response server deadline");
    });
    let client = RestClient::new(
        Url::parse(&format!("http://{address}")).expect("url"),
        "token".into(),
    )
    .expect("rest client");
    let result = client
        .json(
            Method::GET,
            client.url(&["probe"]).expect("probe url"),
            None,
        )
        .await;
    server.await.expect("response server");
    result
}

#[tokio::test]
async fn interrupted_successful_json_body_is_unavailable() {
    let result = read_json_response(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 64\r\n\r\n[",
    )
    .await;
    assert!(
        matches!(result, Err(DaemonError::Unavailable { .. })),
        "an incomplete HTTP body is transport loss: {result:?}"
    );
}

#[tokio::test]
async fn complete_malformed_json_body_stays_protocol_failure() {
    let result = read_json_response(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1\r\n\r\n[",
    )
    .await;
    assert!(
        matches!(result, Err(DaemonError::Protocol { .. })),
        "a complete malformed document remains a contract failure: {result:?}"
    );
}

#[tokio::test]
async fn interrupted_unauthorized_response_stays_unauthorized() {
    let result = read_json_response(
        b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: 64\r\n\r\n[",
    )
    .await;
    assert!(
        matches!(result, Err(DaemonError::Unauthorized)),
        "a refused request must never enter transport recovery: {result:?}"
    );
}

/// A daemon that answers the headers halfway through the deadline and never
/// finishes the body: the call gives up `REQUEST_DEADLINE` after it started,
/// not `REQUEST_DEADLINE` after the headers arrived. The clock is paused
/// only while nothing is on the wire: a paused clock auto-advances past the
/// deadline whenever the runtime idles on socket I/O.
#[tokio::test]
async fn one_deadline_spans_send_and_body() {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let address = listener.local_addr().expect("address");
    let request_read = Arc::new(Notify::new());
    let send_headers = Arc::new(Notify::new());
    let headers_sent = Arc::new(Notify::new());
    let server = {
        let request_read = Arc::clone(&request_read);
        let send_headers = Arc::clone(&send_headers);
        let headers_sent = Arc::clone(&headers_sent);
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("accept");
            let mut request = vec![0_u8; 4096];
            let _ = stream.read(&mut request).await;
            request_read.notify_one();
            send_headers.notified().await;
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 64\r\n\r\n[",
                )
                .await
                .expect("headers");
            headers_sent.notify_one();
            // The body never completes and the connection stays open.
            std::future::pending::<()>().await;
        })
    };
    let client = RestClient::new(
        Url::parse(&format!("http://{address}")).expect("url"),
        "token".into(),
    )
    .expect("rest client");
    let started = Instant::now();
    let mut call = tokio::spawn(async move { client.projects().await });

    request_read.notified().await;
    tokio::time::pause();
    tokio::time::advance(REQUEST_DEADLINE / 2).await;
    tokio::time::resume();
    send_headers.notify_one();
    headers_sent.notified().await;
    // Let the client read the headers before the clock moves on.
    for _ in 0..512 {
        tokio::task::yield_now().await;
    }
    tokio::time::pause();
    tokio::time::advance(REQUEST_DEADLINE / 2 + Duration::from_millis(1)).await;
    tokio::time::resume();

    // A per-phase timeout would still be waiting on the body here.
    let result = tokio::time::timeout(Duration::from_millis(1), &mut call)
        .await
        .expect("the call gave up at the shared deadline")
        .expect("join the call");
    assert!(
        matches!(result, Err(DaemonError::Timeout { .. })),
        "{result:?}"
    );
    assert!(
        started.elapsed() < REQUEST_DEADLINE + REQUEST_DEADLINE / 2,
        "one deadline across headers and body: {:?}",
        started.elapsed()
    );
    server.abort();
}

#[tokio::test]
async fn session_fetch_requests_every_live_daemon_status() {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let mut request = vec![0_u8; 8192];
        let read = stream.read(&mut request).await.expect("read request");
        let body = r#"{"sessions":[],"count":0,"next_cursor":null}"#;
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        stream
            .write_all(response.as_bytes())
            .await
            .expect("write response");
        String::from_utf8_lossy(&request[..read]).into_owned()
    });
    let base = Url::parse(&format!("http://{address}/")).expect("base url");
    let client = RestClient::new(base, "token".to_string()).expect("client");
    client.sessions("project-1").await.expect("sessions");
    let request = server.await.expect("server task");
    let target = request
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .expect("request target");
    let url = Url::parse(&format!("http://{address}{target}")).expect("request url");
    let statuses: Vec<String> = url
        .query_pairs()
        .filter(|(key, _)| key == "status_in")
        .map(|(_, value)| value.into_owned())
        .collect();
    assert_eq!(
        statuses,
        [
            "active",
            "paused",
            "waiting",
            "idle",
            "interrupted",
            "awaiting_input",
            "awaiting_approval",
            "awaiting_handoff",
        ],
        "the sessions fetch must request every daemon live status"
    );
}
