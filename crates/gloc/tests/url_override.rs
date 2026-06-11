use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::Command;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

fn spawn_probe_server() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test probe server");
    let address = listener.local_addr().expect("read test probe address");

    thread::spawn(move || {
        let Ok((mut stream, _)) = listener.accept() else {
            return;
        };

        let mut request = [0_u8; 1024];
        let _ = stream.read(&mut request);
        let response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}";
        let _ = stream.write_all(response);
    });

    format!("http://{}", address)
}

fn unique_temp_dir() -> std::path::PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("gloc-url-override-{}-{nanos}", std::process::id()))
}

#[test]
fn status_uses_url_override_before_backend_validation() {
    let remote_url = spawn_probe_server();
    let temp_dir = unique_temp_dir();
    fs::create_dir_all(&temp_dir).expect("create temp config dir");
    let config_path = temp_dir.join("gloc.yaml");
    fs::write(
        &config_path,
        r#"
settings:
  probe_timeout_ms: 1000
backends:
  - name: ollama
    url: http://127.0.0.1:9
    probe: /api/tags
clients:
  codex:
    binary: codex
    model_flag: --model
    default_model: qwen3-coder
"#,
    )
    .expect("write test config");

    let output = Command::new(env!("CARGO_BIN_EXE_gloc"))
        .args([
            "--config",
            config_path.to_str().expect("utf-8 config path"),
            "--backend",
            "ollama",
            "--url",
            &remote_url,
            "--status",
        ])
        .output()
        .expect("run gloc --status");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        output.status.success(),
        "expected gloc --status to validate override URL\nstdout:\n{stdout}\nstderr:\n{stderr}"
    );
    assert!(stderr.contains(&format!("Backend:  ollama ({remote_url})")));

    fs::remove_dir_all(temp_dir).expect("remove temp config dir");
}
