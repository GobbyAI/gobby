use std::fs;
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;
use std::time::{Duration, Instant};

mod common;

fn gwiki(args: &[&str]) -> std::process::Output {
    common::GwikiFixture::new().output(args)
}

#[test]
fn text_output_uses_renderer() {
    let output = gwiki(&["--format", "text", "search", "--topic", "rust", "ownership"]);

    common::assert_success(&output, "search");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("Search results for \"ownership\""));
    assert!(stdout.contains("Scope: topic:rust"));
    assert!(!stdout.contains("CommandResult"));
    assert!(!stdout.contains("SearchOutput"));
}

#[test]
fn status_goes_to_stderr() {
    let output = gwiki(&["--format", "json", "status", "--topic", "rust"]);

    common::assert_success(&output, "status");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stdout.trim_start().starts_with('{'), "{stdout}");
    assert!(stdout.contains("\"command\": \"status\""), "{stdout}");
    assert!(!stdout.contains("gwiki:"), "{stdout}");
    assert!(
        stderr.contains("gwiki: status resolved scope topic:rust"),
        "{stderr}"
    );
}

#[test]
fn ask_llm_text_output_strips_model_narration() {
    let fixture = common::GwikiFixture::new();
    let init = fixture.output(&["init", "--topic", "rust"]);
    common::assert_success(&init, "topic init");

    let response = r#"{"model":"test-model","choices":[{"message":{"content":"I'm checking the codewiki docs just enough to answer which page types it emits, then I'll summarize the set precisely.I've got the documented page categories already. Hooks run at turn boundaries."}}]}"#;
    let (api_base, request_handle) =
        spawn_chat_completion_server(response).expect("spawn chat completion server");
    write_gcore_yaml(
        fixture.home(),
        &format!(
            "ai:\n  text_generate:\n    routing: direct\n    api_base: {api_base}\n    model: test-model\n"
        ),
    );

    let output = fixture.output(&[
        "--format",
        "text",
        "ask",
        "--topic",
        "rust",
        "--llm",
        "--require-ai",
        "--ai",
        "direct",
        "Which page types does codewiki emit?",
    ]);

    common::assert_success(&output, "ask --llm");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("Answer for"), "{stdout}");
    assert!(stdout.contains("Hooks run at turn boundaries."), "{stdout}");
    assert!(!stdout.contains("I'm checking"), "{stdout}");
    assert!(!stdout.contains("I've got"), "{stdout}");

    let request = request_handle
        .join()
        .expect("chat completion server thread")
        .expect("capture chat completion request");
    assert!(
        request.starts_with("POST /v1/chat/completions HTTP/1.1"),
        "{request}"
    );
    assert!(request.contains("Write only the final answer"), "{request}");
    assert!(
        request.contains("do not describe your process"),
        "{request}"
    );
}

type RequestHandle = thread::JoinHandle<io::Result<String>>;

fn spawn_chat_completion_server(body: &'static str) -> io::Result<(String, RequestHandle)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    listener.set_nonblocking(true)?;
    let addr = listener.local_addr()?;
    let handle = thread::spawn(move || {
        let mut stream = accept_with_timeout(listener, Duration::from_secs(10))?;
        let request = read_http_request(&mut stream)?;
        let response = format!(
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        stream.write_all(response.as_bytes())?;
        Ok(request)
    });
    Ok((format!("http://{addr}"), handle))
}

fn accept_with_timeout(listener: TcpListener, timeout: Duration) -> io::Result<TcpStream> {
    let deadline = Instant::now() + timeout;
    loop {
        match listener.accept() {
            Ok((stream, _)) => {
                // BSD/macOS accepted sockets inherit the listener's
                // non-blocking flag; restore blocking reads for the request.
                stream.set_nonblocking(false)?;
                return Ok(stream);
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "timed out waiting for chat completion request",
                    ));
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(error) => return Err(error),
        }
    }
}

fn read_http_request(stream: &mut TcpStream) -> io::Result<String> {
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    let mut request = Vec::new();
    let mut buffer = [0; 4096];

    loop {
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        request.extend_from_slice(&buffer[..read]);
        if let Some(header_end) = http_header_end(&request) {
            let body_len = content_length(&request[..header_end]).unwrap_or(0);
            if request.len() >= header_end + body_len {
                break;
            }
        }
    }

    Ok(String::from_utf8_lossy(&request).into_owned())
}

fn http_header_end(request: &[u8]) -> Option<usize> {
    request
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|index| index + 4)
}

fn content_length(headers: &[u8]) -> Option<usize> {
    let headers = std::str::from_utf8(headers).ok()?;
    headers.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("content-length")
            .then(|| value.trim().parse().ok())
            .flatten()
    })
}

fn write_gcore_yaml(home: &std::path::Path, contents: &str) {
    let gobby_home = home.join(".gobby");
    fs::create_dir_all(&gobby_home).expect("create isolated gobby home");
    fs::write(gobby_home.join("gcore.yaml"), contents).expect("write isolated gcore config");
}
