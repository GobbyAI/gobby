//! A surviving session's pooled keep-alive connection across a daemon restart.
//!
//! ureq pools the connection a successful request used. Once the daemon process
//! exits, the first request that pulls that socket from the pool must not report
//! DAEMON_UNAVAILABLE while a new daemon already answers on the same port, a
//! daemon that stays down must still be reported promptly, and a non-idempotent
//! call that reached the daemon must never be replayed.

use gobby_mcp::daemon::{Dialer, RequestOptions};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::fs;
use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const TOOL_PATH: &str = "/api/mcp/gobby-agents/tools/wait_for_coordination/";

fn isolated(test: impl FnOnce(&std::path::Path) -> anyhow::Result<()>) -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    fs::write(home.path().join("bootstrap.yaml"), "api_key: first\n")?;
    temp_env::with_vars(
        [
            ("GOBBY_HOME", home.path().to_str()),
            ("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", None),
            ("GOBBY_MANAGED_EXECUTION_ID", None),
            ("GOBBY_AGENT_API_TOKEN", None),
            ("GOBBY_AGENT_RUN_ID", None),
            ("GOBBY_SESSION_ID", None),
            ("GOBBY_PROJECT_ID", None),
            ("GOBBY_RELEASE_TOKEN", None),
        ],
        || test(home.path()),
    )
}

fn free_address() -> anyhow::Result<SocketAddr> {
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?)
}

fn direct() -> RequestOptions<'static> {
    RequestOptions {
        preflight: false,
        ..Default::default()
    }
}

fn preflighted() -> RequestOptions<'static> {
    RequestOptions {
        preflight: true,
        ..Default::default()
    }
}

/// How the stub process leaves its sockets when it exits: a graceful close
/// sends FIN; a reset sends RST, as a killed process with lingering data does.
#[derive(Clone, Copy)]
enum Exit {
    Close,
    Reset,
}

enum Reply {
    Json(u16, Value),
    /// Read the request fully, then drop the connection without answering.
    Vanish,
}

#[derive(Clone, Debug)]
struct Served {
    connection: usize,
    method: String,
    path: String,
}

/// An HTTP/1.1 keep-alive daemon: every connection serves requests until the
/// client closes it or the daemon exits, so the dialer's pool really reuses it.
struct KeepAliveDaemon {
    stop: Arc<AtomicBool>,
    served: Arc<Mutex<Vec<Served>>>,
    worker: Option<JoinHandle<()>>,
}

impl KeepAliveDaemon {
    fn bind(address: SocketAddr, exit: Exit, replies: Vec<Reply>) -> anyhow::Result<Self> {
        let listener = TcpListener::bind(address)?;
        listener.set_nonblocking(true)?;
        let stop = Arc::new(AtomicBool::new(false));
        let served = Arc::new(Mutex::new(Vec::new()));
        let replies = Arc::new(Mutex::new(VecDeque::from(replies)));
        let worker = thread::spawn({
            let stop = stop.clone();
            let served = served.clone();
            move || {
                let mut handlers = Vec::new();
                while !stop.load(Ordering::SeqCst) {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            let connection = handlers.len();
                            // Accepted sockets inherit nonblocking mode on macOS.
                            stream.set_nonblocking(false).unwrap();
                            stream
                                .set_read_timeout(Some(Duration::from_millis(20)))
                                .unwrap();
                            if matches!(exit, Exit::Reset) {
                                socket2::SockRef::from(&stream)
                                    .set_linger(Some(Duration::ZERO))
                                    .unwrap();
                            }
                            let stop = stop.clone();
                            let served = served.clone();
                            let replies = replies.clone();
                            handlers.push(thread::spawn(move || {
                                serve(stream, connection, &stop, &served, &replies)
                            }));
                        }
                        Err(error) if error.kind() == ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(2));
                        }
                        Err(error) => panic!("stub accept: {error}"),
                    }
                }
                drop(listener);
                for handler in handlers {
                    handler.join().unwrap();
                }
            }
        });
        Ok(Self {
            stop,
            served,
            worker: Some(worker),
        })
    }

    fn served(&self) -> Vec<Served> {
        self.served.lock().unwrap().clone()
    }

    fn paths(&self) -> Vec<String> {
        self.served().into_iter().map(|s| s.path).collect()
    }

    /// The process exits: every accepted socket closes the configured way and
    /// the port is released for the next daemon.
    fn exit(mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.worker.take().unwrap().join().unwrap();
        // Loopback delivery is asynchronous; let the FIN or RST land on the
        // client's pooled socket before the next request inspects it.
        thread::sleep(Duration::from_millis(50));
    }
}

impl Drop for KeepAliveDaemon {
    fn drop(&mut self) {
        if let Some(worker) = self.worker.take() {
            self.stop.store(true, Ordering::SeqCst);
            worker.join().unwrap();
        }
    }
}

fn serve(
    stream: TcpStream,
    connection: usize,
    stop: &AtomicBool,
    served: &Mutex<Vec<Served>>,
    replies: &Mutex<VecDeque<Reply>>,
) {
    let mut reader = BufReader::new(stream);
    while let Some((method, path)) = read_request(&mut reader, stop) {
        served.lock().unwrap().push(Served {
            connection,
            method,
            path,
        });
        let reply = replies
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected request to keep-alive stub");
        match reply {
            Reply::Json(status, body) => {
                let body = body.to_string();
                let wire = format!(
                    "HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n{body}",
                    body.len()
                );
                if reader.get_mut().write_all(wire.as_bytes()).is_err() {
                    return;
                }
            }
            Reply::Vanish => return,
        }
    }
}

/// One request's method and path, with its body drained. None once the peer
/// closed the connection or the daemon is exiting.
fn read_request(reader: &mut BufReader<TcpStream>, stop: &AtomicBool) -> Option<(String, String)> {
    let request_line = next_line(reader, stop)?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap().to_owned();
    let path = parts.next().unwrap().to_owned();
    let mut length = 0;
    loop {
        let header = next_line(reader, stop)?;
        if header.trim().is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':')
            && name.eq_ignore_ascii_case("content-length")
        {
            length = value.trim().parse().unwrap();
        }
    }
    let mut body = vec![0; length];
    let mut filled = 0;
    while filled < length {
        match reader.read(&mut body[filled..]) {
            Ok(0) => return None,
            Ok(count) => filled += count,
            Err(error) if is_read_timeout(&error) => {
                if stop.load(Ordering::SeqCst) {
                    return None;
                }
            }
            Err(error) => panic!("stub body read: {error}"),
        }
    }
    Some((method, path))
}

fn next_line(reader: &mut BufReader<TcpStream>, stop: &AtomicBool) -> Option<String> {
    let mut line = String::new();
    loop {
        match reader.read_line(&mut line) {
            Ok(0) => return None,
            Ok(_) => return Some(line),
            Err(error) if is_read_timeout(&error) => {
                if stop.load(Ordering::SeqCst) {
                    return None;
                }
            }
            Err(error) => panic!("stub header read: {error}"),
        }
    }
}

fn is_read_timeout(error: &std::io::Error) -> bool {
    matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut)
}

/// Two direct requests pool a keep-alive connection, the daemon exits the given
/// way, a new daemon binds the same port, and the session's first preflighted
/// call's result and the paths the new daemon served are returned for the caller to check.
fn first_call_after_restart(exit: Exit) -> anyhow::Result<(serde_json::Value, Vec<String>)> {
    let mut outcome = None;
    isolated(|root| {
        let address = free_address()?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(format!("http://{address}")),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                let before = KeepAliveDaemon::bind(
                    address,
                    exit,
                    vec![
                        Reply::Json(200, json!({"n":1})),
                        Reply::Json(200, json!({"n":2})),
                    ],
                )?;
                assert_eq!(
                    dialer.request("GET", "/one", None, direct()),
                    json!({"n":1})
                );
                assert_eq!(
                    dialer.request("GET", "/two", None, direct()),
                    json!({"n":2})
                );
                let served = before.served();
                assert_eq!(before.paths(), ["/one", "/two"]);
                assert_eq!(
                    served[0].connection, served[1].connection,
                    "the second request must reuse the pooled keep-alive connection: {served:?}"
                );
                before.exit();

                let after = KeepAliveDaemon::bind(
                    address,
                    exit,
                    vec![
                        Reply::Json(200, json!({"status":"healthy"})),
                        Reply::Json(200, json!({"restarted":true})),
                    ],
                )?;
                let result = dialer.request("POST", TOOL_PATH, Some(&json!({})), preflighted());
                outcome = Some((result, after.paths()));
                Ok(())
            },
        )
    })?;
    outcome.ok_or_else(|| anyhow::anyhow!("the restart scenario recorded no outcome"))
}

#[test]
fn first_call_after_graceful_restart_succeeds_on_fresh_connection() -> anyhow::Result<()> {
    let (result, paths) = first_call_after_restart(Exit::Close)?;
    assert_eq!(result, json!({"restarted":true}), "{result}");
    assert_eq!(paths, ["/api/health", TOOL_PATH]);
    Ok(())
}

#[test]
fn first_call_after_reset_restart_succeeds_on_fresh_connection() -> anyhow::Result<()> {
    let (result, paths) = first_call_after_restart(Exit::Reset)?;
    assert_eq!(result, json!({"restarted":true}), "{result}");
    assert_eq!(paths, ["/api/health", TOOL_PATH]);
    Ok(())
}

#[test]
fn daemon_that_stays_down_is_reported_unavailable_promptly() -> anyhow::Result<()> {
    isolated(|root| {
        let address = free_address()?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(format!("http://{address}")),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                let before = KeepAliveDaemon::bind(
                    address,
                    Exit::Reset,
                    vec![Reply::Json(200, json!({"n":1}))],
                )?;
                assert_eq!(
                    dialer.request("GET", "/one", None, direct()),
                    json!({"n":1})
                );
                before.exit();

                let started = Instant::now();
                let result = dialer.request("POST", TOOL_PATH, Some(&json!({})), preflighted());
                let elapsed = started.elapsed();
                assert_eq!(result["error_code"], "DAEMON_UNAVAILABLE", "{result}");
                assert!(
                    elapsed < Duration::from_secs(1),
                    "a daemon that stays down must be reported within the health budget, took {elapsed:?}"
                );
                Ok(())
            },
        )
    })
}

#[test]
fn non_idempotent_call_that_reached_the_daemon_is_not_replayed() -> anyhow::Result<()> {
    isolated(|root| {
        let address = free_address()?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(format!("http://{address}")),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                let daemon = KeepAliveDaemon::bind(
                    address,
                    Exit::Close,
                    vec![
                        Reply::Json(200, json!({"first":true})),
                        Reply::Vanish,
                        Reply::Json(200, json!({"replayed":true})),
                    ],
                )?;
                assert_eq!(
                    dialer.request("POST", TOOL_PATH, Some(&json!({"attempt":1})), direct()),
                    json!({"first":true})
                );
                let lost = dialer.request("POST", TOOL_PATH, Some(&json!({"attempt":2})), direct());
                assert_eq!(lost["success"], false, "{lost}");
                assert_eq!(lost["error_code"], "DAEMON_UNAVAILABLE", "{lost}");
                let served = daemon.served();
                assert_eq!(
                    served.iter().filter(|s| s.method == "POST").count(),
                    2,
                    "a POST that reached the daemon must not be replayed: {served:?}"
                );
                Ok(())
            },
        )
    })
}
