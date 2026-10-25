//! `gmcp` starts a stopped local daemon once, at launch, through the recorded start command.

#[allow(
    dead_code,
    reason = "shared stub; startup probes are checked by method and path only"
)]
mod stub_daemon;

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
use std::net::{SocketAddr, TcpListener};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};
use stub_daemon::{Response, StubDaemon};

const PROTOCOL_VERSION: &str = "2025-11-25";

/// A stdio `gmcp` child whose stderr log lines the test can read.
struct Gmcp {
    child: Child,
    stdin: Option<ChildStdin>,
    messages: mpsc::Receiver<Value>,
    stderr: Arc<Mutex<Vec<String>>>,
}

impl Gmcp {
    fn spawn(daemon_url: &str, home: &Path, env: &[(&str, &str)]) -> Result<Self> {
        std::fs::write(home.join("bootstrap.yaml"), "api_key: test\n")?;
        let mut command = Command::new(env!("CARGO_BIN_EXE_gmcp"));
        command
            .current_dir(home)
            .env("GOBBY_HOME", home)
            .env("GOBBY_DAEMON_URL", daemon_url)
            .env_remove("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
            .env_remove("GOBBY_MANAGED_EXECUTION_ID")
            .env_remove("GOBBY_AGENT_API_TOKEN")
            .env_remove("GOBBY_AGENT_RUN_ID")
            .env_remove("GOBBY_SESSION_ID")
            .env_remove("GOBBY_PROJECT_ID")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for (name, value) in env {
            command.env(name, value);
        }
        let mut child = command.spawn().context("spawn gmcp")?;
        let stdout = child.stdout.take().context("gmcp stdout")?;
        let (sender, messages) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Ok(message) = serde_json::from_str::<Value>(&line)
                    && sender.send(message).is_err()
                {
                    break;
                }
            }
        });
        let stderr_pipe = child.stderr.take().context("gmcp stderr")?;
        let stderr = Arc::new(Mutex::new(Vec::new()));
        let lines = Arc::clone(&stderr);
        thread::spawn(move || {
            for line in BufReader::new(stderr_pipe).lines().map_while(Result::ok) {
                lines.lock().unwrap().push(line);
            }
        });
        let stdin = child.stdin.take();
        Ok(Self {
            child,
            stdin,
            messages,
            stderr,
        })
    }

    fn send(&mut self, message: &Value) -> Result<()> {
        let stdin = self.stdin.as_mut().context("gmcp stdin is closed")?;
        writeln!(stdin, "{message}")?;
        stdin.flush()?;
        Ok(())
    }

    fn initialize(&mut self) -> Result<Value> {
        self.send(&json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "daemon-start", "version": "0"},
            },
        }))?;
        loop {
            let message = self
                .messages
                .recv_timeout(Duration::from_secs(10))
                .context("no answer to initialize")?;
            if message.get("id") == Some(&json!(1)) {
                self.send(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}))?;
                return Ok(message["result"].clone());
            }
        }
    }

    fn stderr_lines(&self) -> Vec<String> {
        self.stderr.lock().unwrap().clone()
    }

    /// Wait for a stderr line the predicate accepts and return it.
    fn wait_for_log(&self, limit: Duration, accept: impl Fn(&str) -> bool) -> Result<String> {
        let deadline = Instant::now() + limit;
        loop {
            if let Some(line) = self.stderr_lines().into_iter().find(|line| accept(line)) {
                return Ok(line);
            }
            if Instant::now() >= deadline {
                bail!(
                    "no matching gmcp log within {limit:?}; stderr: {:?}",
                    self.stderr_lines()
                );
            }
            thread::sleep(Duration::from_millis(20));
        }
    }

    fn close_stdin(&mut self) {
        self.stdin = None;
    }

    fn wait_for_exit(&mut self, limit: Duration) -> Result<ExitStatus> {
        let deadline = Instant::now() + limit;
        loop {
            if let Some(status) = self.child.try_wait()? {
                return Ok(status);
            }
            if Instant::now() >= deadline {
                bail!("gmcp did not exit within {limit:?} of stdin closing");
            }
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn is_running(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }
}

impl Drop for Gmcp {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// A loopback address with nothing listening, so every dial is refused until a stub binds it.
fn closed_address() -> Result<SocketAddr> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let address = listener.local_addr()?;
    drop(listener);
    Ok(address)
}

/// Record `body` as the home's start command: `/bin/sh <script>`, the shape 1.4's writer leaves.
fn record_start_script(home: &Path, body: &str) -> Result<()> {
    let script = home.join("start.sh");
    std::fs::write(&script, body)?;
    let argv = json!(["/bin/sh", script.to_string_lossy()]);
    std::fs::write(home.join("gmcp-daemon-start.json"), argv.to_string())?;
    Ok(())
}

fn wait_for_file(path: &Path, limit: Duration) -> Result<String> {
    let deadline = Instant::now() + limit;
    loop {
        if let Ok(text) = std::fs::read_to_string(path)
            && !text.is_empty()
        {
            return Ok(text);
        }
        if Instant::now() >= deadline {
            bail!("{} did not appear within {limit:?}", path.display());
        }
        thread::sleep(Duration::from_millis(20));
    }
}

/// A log line that reports the started daemon healthy, not one reporting it unhealthy.
fn reports_healthy(line: &str) -> bool {
    let line = line.to_ascii_lowercase();
    line.contains("healthy") && !line.contains("unhealthy") && !line.contains("not ")
}

/// Watch for `window`: a start command that must not run never creates its marker.
fn assert_absent_for(path: &Path, window: Duration, why: &str) {
    let deadline = Instant::now() + window;
    while Instant::now() < deadline {
        assert!(!path.exists(), "{why}");
        thread::sleep(Duration::from_millis(20));
    }
}

fn marker(home: &Path) -> PathBuf {
    home.join("ran")
}

/// A start command that only records that it ran.
fn record_marker_command(home: &Path) -> Result<()> {
    record_start_script(home, &format!("echo run >> '{}'\n", marker(home).display()))
}

#[test]
fn recorded_command_starts_unreachable_local_daemon() -> Result<()> {
    let home = tempfile::tempdir()?;
    let home = home.path();
    let ran = marker(home);
    let go = home.join("go");
    let done = home.join("done");
    // The command records its stdin and output kinds, then holds until the test says go.
    record_start_script(
        home,
        &format!(
            "if read -r line; then s=data; else s=eof; fi\n\
             if [ -f /dev/fd/1 ]; then o=file; else o=other; fi\n\
             if [ -f /dev/fd/2 ]; then e=file; else e=other; fi\n\
             echo \"stdin=$s stdout=$o stderr=$e\" >> '{ran}'\n\
             i=0; while [ ! -e '{go}' ] && [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n\
             echo done >> '{done}'\n",
            ran = ran.display(),
            go = go.display(),
            done = done.display(),
        ),
    )?;
    let address = closed_address()?;
    let mut gmcp = Gmcp::spawn(&format!("http://{address}"), home, &[])?;

    gmcp.initialize()?;
    assert!(!done.exists(), "initialize waited for the start command");
    assert_eq!(
        wait_for_file(&ran, Duration::from_secs(5))?,
        "stdin=eof stdout=file stderr=file\n"
    );

    let daemon = StubDaemon::bind(
        address,
        (0..10)
            .map(|_| Response::json(200, json!({"status": "healthy"})))
            .collect(),
    )?;
    std::fs::write(&go, "")?;
    wait_for_file(&done, Duration::from_secs(5))?;
    gmcp.wait_for_log(Duration::from_secs(15), reports_healthy)?;

    assert_eq!(std::fs::read_to_string(&ran)?.lines().count(), 1);
    let requests = daemon.requests();
    assert!(!requests.is_empty());
    for request in requests {
        assert_eq!(
            (request.method.as_str(), request.path.as_str()),
            ("GET", "/api/health")
        );
    }
    Ok(())
}

#[test]
fn no_start_without_cause() -> Result<()> {
    let typed_unavailable = json!({"status": "unavailable", "backend": {"state": "starting"}});
    for (status, body) in [
        (200, json!({"status": "healthy"})),
        (503, typed_unavailable),
    ] {
        let home = tempfile::tempdir()?;
        record_marker_command(home.path())?;
        let daemon = StubDaemon::start(vec![Response::json(status, body)])?;
        let mut gmcp = Gmcp::spawn(&daemon.url(), home.path(), &[])?;
        // One probe answers, so a daemon is running; the stub refuses any second request.
        daemon.wait_for_requests(1);
        if status == 503 {
            // Any answer other than healthy is a running daemon, logged with its status.
            gmcp.wait_for_log(Duration::from_secs(5), |line| line.contains("503"))?;
        }
        assert_absent_for(
            &marker(home.path()),
            Duration::from_millis(1_500),
            &format!("started a daemon after HTTP {status}"),
        );
        assert!(gmcp.is_running());
        let requests = daemon.requests();
        assert_eq!(requests.len(), 1, "HTTP {status}");
        assert_eq!(requests[0].path, "/api/health");
    }

    // A managed agent process never starts a daemon.
    let home = tempfile::tempdir()?;
    record_marker_command(home.path())?;
    let mut gmcp = Gmcp::spawn(
        &format!("http://{}", closed_address()?),
        home.path(),
        &[("GOBBY_AGENT_RUN_ID", "run-1")],
    )?;
    assert_absent_for(
        &marker(home.path()),
        Duration::from_secs(2),
        "an agent process started a daemon",
    );
    assert!(gmcp.is_running());

    // A non-local dial target is never started.
    let home = tempfile::tempdir()?;
    record_marker_command(home.path())?;
    let mut gmcp = Gmcp::spawn("http://daemon.invalid:1", home.path(), &[])?;
    assert_absent_for(
        &marker(home.path()),
        Duration::from_secs(3),
        "started a daemon for a remote target",
    );
    assert!(gmcp.is_running());

    // No record: nothing to run, and the log says how to start the daemon.
    let home = tempfile::tempdir()?;
    let gmcp = Gmcp::spawn(&format!("http://{}", closed_address()?), home.path(), &[])?;
    gmcp.wait_for_log(Duration::from_secs(5), |line| line.contains("gobby start"))?;
    Ok(())
}

#[test]
fn stdin_close_leaves_start_running() -> Result<()> {
    let home = tempfile::tempdir()?;
    let home = home.path();
    let started = home.join("started");
    let done = home.join("done");
    record_start_script(
        home,
        &format!(
            "echo started >> '{started}'\nsleep 3\necho done >> '{done}'\n",
            started = started.display(),
            done = done.display(),
        ),
    )?;
    let mut gmcp = Gmcp::spawn(&format!("http://{}", closed_address()?), home, &[])?;
    gmcp.initialize()?;
    wait_for_file(&started, Duration::from_secs(5))?;

    gmcp.close_stdin();

    let status = gmcp.wait_for_exit(Duration::from_secs(2))?;
    assert!(status.success(), "gmcp exited with {status}");
    assert!(
        !done.exists(),
        "the start command finished before gmcp exited"
    );
    assert_eq!(wait_for_file(&done, Duration::from_secs(8))?, "done\n");
    Ok(())
}
