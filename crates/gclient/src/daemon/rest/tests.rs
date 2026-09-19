use super::*;
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::Notify;

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
