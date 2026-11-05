use super::*;
use crate::app::Workspace;
use crate::daemon::{Checkout, ProjectRow, SidebarRows};
use crate::ui::chrome::Chrome;
use crate::ui::chrome_render::render_workspace;
use crate::ui::sidebar::agent_rows;
use ratatui::{backend::TestBackend, layout::Rect, Terminal};
use serde_json::json;
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

/// An idle or waiting session is parked between turns, still live: the
/// sidebar takes its ref from these rows, so the fetch must ask for every
/// live status the daemon writes.
#[tokio::test]
async fn sessions_fetch_asks_for_every_live_status() {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let mut request = Vec::new();
        let mut chunk = [0_u8; 4096];
        while !request.windows(4).any(|boundary| boundary == b"\r\n\r\n") {
            let count = stream.read(&mut chunk).await.expect("read request");
            assert_ne!(count, 0, "request closed before headers completed");
            request.extend_from_slice(&chunk[..count]);
        }
        let body = br#"{"sessions":[]}"#;
        let head = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
            body.len()
        );
        stream.write_all(head.as_bytes()).await.expect("write head");
        stream.write_all(body).await.expect("write body");
        stream.shutdown().await.expect("end response");
        String::from_utf8(request).expect("utf8 request")
    });
    let client = RestClient::new(
        Url::parse(&format!("http://{address}")).expect("url"),
        "token".into(),
    )
    .expect("rest client");
    client.sessions("project-1").await.expect("sessions");
    let request = server.await.expect("request server");
    let target = request.lines().next().expect("request line");
    let url = Url::parse(&format!(
        "http://host{}",
        target.split(' ').nth(1).expect("request target")
    ))
    .expect("target url");
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
        ]
    );
}

#[tokio::test]
async fn an_idle_session_fetched_from_the_daemon_renders_its_ref_on_the_sidebar() {
    // The daemon returns a session only for a status the request names, so the
    // idle seat reaches the sidebar only when the fetch asks for idle.
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let mut request = vec![0_u8; 8192];
        let read = stream.read(&mut request).await.expect("read request");
        let text = String::from_utf8_lossy(&request[..read]).into_owned();
        let target = text
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .expect("request target");
        let url = Url::parse(&format!("http://{address}{target}")).expect("request url");
        let asks_idle = url
            .query_pairs()
            .any(|(key, value)| key == "status_in" && value == "idle");
        let body = if asks_idle {
            r##"{"sessions":[{"id":"sess-idle","ref":"#41","status":"idle","source":"codex","model":"gpt-5"}],"count":1,"next_cursor":null}"##
        } else {
            r##"{"sessions":[],"count":0,"next_cursor":null}"##
        };
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        stream
            .write_all(response.as_bytes())
            .await
            .expect("write response");
    });
    let base = Url::parse(&format!("http://{address}/")).expect("base url");
    let client = RestClient::new(base, "token".to_string()).expect("client");
    let sessions = client.sessions("project-1").await.expect("sessions");
    server.await.expect("server task");

    let mut ws = Workspace::scripted();
    ws.set_local_machine("local");
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: vec![ProjectRow {
            id: "project-1".to_string(),
            name: "alpha".to_string(),
            display_name: "alpha".to_string(),
            checkout: Some(Checkout {
                machine_id: "local".to_string(),
                root_path: "/repos/alpha".to_string(),
            }),
            ..ProjectRow::default()
        }],
        sessions: [("project-1".to_string(), sessions)].into_iter().collect(),
        ..SidebarRows::default()
    });
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "session:sess-idle",
            "session_id": "sess-idle",
            "provider": "codex",
            "model": "gpt-5",
            "terminal": {"terminal_id": "term-idle", "backend": "native"}
        }]
    }));
    ws.select_project("project-1");
    ws.reconcile_subscribe_first().expect("install roster");
    ws.open_terminal("term-idle", "native", "epoch")
        .expect("open idle terminal");

    let mut chrome = Chrome::dark();
    chrome.sidebar.pinned = true;
    chrome.sidebar.width = 34;
    let rows = agent_rows(&ws, &chrome);
    let references: Vec<&str> = rows.iter().map(|row| row.reference.as_str()).collect();
    assert!(
        references.contains(&"#41"),
        "the idle agent row must carry its ref: {references:?}"
    );

    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test backend");
    terminal
        .draw(|frame| {
            render_workspace(frame, &ws, &chrome);
        })
        .expect("draw frame");
    let buffer = terminal.backend().buffer();
    let sidebar: Vec<String> = (0..40_u16)
        .map(|y| (0..34_u16).map(|x| buffer[(x, y)].symbol()).collect())
        .collect();
    assert!(
        sidebar.iter().any(|line| line.contains("#41")),
        "the idle seat's ref must render on the sidebar:\n{}",
        sidebar.join("\n")
    );
}
