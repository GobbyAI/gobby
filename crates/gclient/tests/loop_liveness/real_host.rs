//! Explicit release probe: real gterm, PTYs and rendered client echo in temporary state.
use super::*;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};

const CONTROL_TOKEN: &str = "isolated-liveness-control";
const CHILD: &str = r#"
use strict; use warnings; use IO::Socket::UNIX; use IO::Handle;
my ($path, $history) = @ARGV;
my $receipt = IO::Socket::UNIX->new(Type => SOCK_STREAM, Peer => $path) or die $!;
STDOUT->autoflush(1); $receipt->autoflush(1);
print STDOUT "history $_ abcdefghijklmnopqrstuvwxyz\r\n" for 1..$history;
print STDOUT "\r\e[2Kready";
print $receipt "R";
my $n = 0;
while (sysread(STDIN, my $ch, 1)) {
    print $receipt $ch;
    ++$n; print STDOUT "\r\e[2Ke$n$ch";
}
"#;

struct Control {
    stream: BufReader<UnixStream>,
    seq: u64,
}

impl Control {
    async fn connect(path: &std::path::Path) -> Self {
        let stream = timeout(Duration::from_secs(5), async {
            loop {
                match UnixStream::connect(path).await {
                    Ok(stream) => break stream,
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::NotFound | std::io::ErrorKind::ConnectionRefused
                        ) =>
                    {
                        tokio::time::sleep(Duration::from_millis(5)).await;
                    }
                    Err(error) => panic!("connect {}: {error}", path.display()),
                }
            }
        })
        .await
        .unwrap_or_else(|_| {
            panic!(
                "host not ready: {}",
                std::fs::read_to_string(path.parent().unwrap().join("stderr")).unwrap_or_default()
            )
        });
        let mut control = Self {
            stream: BufReader::new(stream),
            seq: 0,
        };
        control
            .rpc(
                "hello",
                json!({"protocol_version":1,"control_token":CONTROL_TOKEN}),
            )
            .await;
        control
    }
    async fn rpc(&mut self, method: &str, mut request: Value) -> Value {
        self.seq += 1;
        request["method"] = json!(method);
        request["id"] = json!(self.seq);
        self.stream
            .get_mut()
            .write_all(format!("{request}\n").as_bytes())
            .await
            .unwrap();
        let mut line = String::new();
        timeout(Duration::from_secs(15), self.stream.read_line(&mut line))
            .await
            .unwrap()
            .unwrap();
        let reply: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(reply["id"], self.seq, "{reply}");
        if method == "snapshot" {
            assert!(
                reply["ok"] == true
                    || matches!(
                        reply["error"].as_str(),
                        Some("capture_busy" | "capture_invalidated")
                    ),
                "snapshot: {reply}"
            );
        } else {
            assert_eq!(reply["ok"], true, "{method}: {reply}");
        }
        reply
    }
}

struct Pane {
    id: String,
    host_id: String,
    pgid: i32,
    receipt: UnixStream,
}

struct RealHost {
    child: tokio::process::Child,
    home: tempfile::TempDir,
    control: Control,
    epoch: String,
    panes: Vec<Pane>,
}

impl Drop for RealHost {
    fn drop(&mut self) {
        for pane in &self.panes {
            // Every group was created by this isolated fixture.
            unsafe {
                libc::kill(-pane.pgid, libc::SIGKILL);
            }
        }
        let _ = self.child.start_kill();
    }
}

impl RealHost {
    async fn start(count: usize) -> Self {
        use std::os::unix::fs::PermissionsExt;
        let binary = std::env::var_os("GOBBY_TEST_GTERM_BIN")
            .expect("build gterm explicitly and set GOBBY_TEST_GTERM_BIN to that artifact");
        let home = tempfile::Builder::new()
            .prefix("gterm-live-")
            .tempdir_in("/tmp")
            .unwrap();
        let root = home.path();
        std::fs::write(root.join("bootstrap.yaml"), "api_key: local-token\n").unwrap();
        std::fs::write(root.join("gterm-control.token"), CONTROL_TOKEN).unwrap();
        std::fs::set_permissions(
            root.join("gterm-control.token"),
            std::fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        std::fs::write(root.join("echo.pl"), CHILD).unwrap();
        let child = tokio::process::Command::new(binary)
            .arg("host")
            .arg("--socket-dir")
            .arg(root)
            .env("GOBBY_HOME", root)
            .env("GTERM_TEST_HELPER", "1")
            .env("GTERM_LOG_FILE", root.join("gterm.log"))
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::from(
                std::fs::File::create(root.join("stderr")).unwrap(),
            ))
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        wait_within(Duration::from_secs(60), "isolated host socket", || {
            root.join("gterm-control.sock").exists()
        })
        .await;
        let mut control = Control::connect(&root.join("gterm-control.sock")).await;
        let ping = control.rpc("ping", json!({})).await;
        let epoch = ping["host_epoch"].as_str().unwrap().to_owned();
        let listener = UnixListener::bind(root.join("receipt.sock")).unwrap();
        let mut panes = Vec::new();
        for index in 0..count {
            let id = format!("live-{index}");
            let reserved = control
                .rpc(
                    "reserve_observer",
                    json!({"terminal_id":id,"reserve_key":id}),
                )
                .await;
            let history = if index == 0 || index + 1 == count {
                20_000
            } else {
                0
            };
            let spawned = control.rpc("spawn", json!({"terminal_id":id,"spawn_key":id,
                "operation_seq":index+1,"reservation_id":reserved["reservation_id"],"reserve_key":id,
                "argv":["/bin/sh","-c","stty raw -echo; exec \"$@\"", "fixture",
                    "/usr/bin/perl", root.join("echo.pl"), root.join("receipt.sock"), history.to_string()],
                "cwd":root,"rows":24,"cols":80,"commit_deadline_ms":15000})).await;
            control
                .rpc("spawn_commit", json!({"terminal_id":id,"spawn_key":id}))
                .await;
            let (mut receipt, _) = timeout(Duration::from_secs(15), listener.accept())
                .await
                .unwrap()
                .unwrap();
            let mut ready = [0];
            timeout(Duration::from_secs(15), receipt.read_exact(&mut ready))
                .await
                .unwrap()
                .unwrap();
            assert_eq!(ready, [b'R']);
            let host_id = spawned["host_terminal_id"].as_str().unwrap().to_owned();
            panes.push(Pane {
                id: host_id.clone(),
                host_id,
                pgid: spawned["pgid"].as_i64().unwrap() as i32,
                receipt,
            });
        }
        Self {
            child,
            home,
            control,
            epoch,
            panes,
        }
    }

    fn attach(&self, pane: &Pane) -> Value {
        json!({"backend":"native","frame_host_epoch":self.epoch,
            "host_socket":self.home.path().join("gterm-frames.sock"),"host_terminal_id":pane.host_id})
    }
    fn locator(&self, pane: &Pane) -> Value {
        json!({"host_epoch":self.epoch,"host_terminal_id":pane.host_id,
            "frame_socket_path":self.home.path().join("gterm-frames.sock"),"pane":null})
    }

    async fn backed_up_consumers(&self) -> (Control, UnixStream) {
        let mut events = Control::connect(&self.home.path().join("gterm-control.sock")).await;
        events.rpc("subscribe_events", json!({})).await;
        let mut stream = UnixStream::connect(self.home.path().join("gterm-frames.sock"))
            .await
            .unwrap();
        write_message_async(
            &mut stream,
            &ClientMessage::Hello {
                version: 1,
                encoding: gobby_terminal::protocol::RenderEncoding::SemanticFrame,
                local_token: "local-token".into(),
                cols: 80,
                rows: 24,
                tmux_identity: None,
            },
        )
        .await
        .unwrap();
        assert!(matches!(
            read_message_async::<_, ServerMessage>(&mut stream, MAX_FRAME_SIZE)
                .await
                .unwrap(),
            ServerMessage::Welcome { .. }
        ));
        write_message_async(
            &mut stream,
            &ClientMessage::AttachTerminal {
                host_terminal_id: self.panes[0].host_id.clone(),
                reservation_id: None,
                locator: None,
            },
        )
        .await
        .unwrap();
        timeout(Duration::from_secs(5), async {
            loop {
                match read_message_async::<_, ServerMessage>(&mut stream, MAX_FRAME_SIZE)
                    .await
                    .unwrap()
                {
                    ServerMessage::Attached { .. } => break,
                    ServerMessage::Error { code, message } => {
                        panic!("observer attach refused: {code} {message:?}")
                    }
                    _ => {}
                }
            }
        })
        .await
        .expect("observer attach deadline");
        (events, stream)
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn real_pty_delivery_and_rendered_echo_by_pane_count() {
    for count in [1, 11, 51, 101] {
        eprintln!("starting isolated {count}-pane probe");
        let mock = MockDaemon::start("local-token").await;
        let mut host = RealHost::start(count).await;
        mock.use_unique_attachment_ids();
        let items: Vec<Value> = host.panes.iter().map(|pane| {
            mock.serve_direct_attach(host.locator(pane));
            json!({"terminal_id":pane.id,"backend":"native","state":"live","attach":host.attach(pane)})
        }).collect();
        mock.seed_terminal_inventory(items.clone());
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({"items":items,"next_cursor":null,"snapshot":{"daemon_epoch":"epoch-1","seq":1}}),
        );
        let ids: Vec<_> = host.panes.iter().map(|pane| pane.id.as_str()).collect();
        mock.seed_balanced_workspace("project-1", &[(ids.as_slice(), ids[0])]);
        let daemon = LiveDaemon::connect(mock.url(), "local-token")
            .await
            .unwrap();
        let mut workspace = Workspace::live(daemon);
        workspace.set_gobby_home(host.home.path().to_path_buf());
        workspace.select_project("project-1");
        let (backend, draws, frames) = DrawRecorder::new(LATENCY_COLS, LATENCY_ROWS);
        let draw_notify = backend.draw_notify.clone();
        let mut terminal = Terminal::new(backend).unwrap();
        let mut chrome = Chrome::dark();
        let (input_tx, input_rx) = mpsc::channel(1);
        let driver = async {
            wait_within(
                Duration::from_secs(15),
                "all native PTY size claims",
                || resized_terminals(&mock) == count,
            )
            .await;
            // The mock records daemon requests but has no PTY actor. Apply its
            // size claims to the isolated host, as the real daemon does.
            let resizes = websocket_requests(&mock, "terminal_resize");
            for (index, pane) in host.panes.iter().enumerate() {
                let size = resizes
                    .iter()
                    .rev()
                    .find(|request| request["terminal_id"].as_str() == Some(pane.id.as_str()))
                    .expect("shown pane has a PTY size claim");
                host.control
                    .rpc(
                        "resize",
                        json!({
                            "host_terminal_id":pane.host_id,
                            "operation_seq":count + index + 1,
                            "rows":size["rows"], "cols":size["cols"]
                        }),
                    )
                    .await;
            }
            wait_within(Duration::from_secs(15), "initial native frame", || {
                frames
                    .lock()
                    .unwrap()
                    .last()
                    .is_some_and(|frame| frame.contains("ready"))
            })
            .await;
            let (_events, _observer) = host.backed_up_consumers().await;
            let handshakes = mock.websocket_handshakes();
            let inventory_reads = || {
                mock.requests()
                    .iter()
                    .filter(|request| {
                        request.method == "GET" && request.target.starts_with("/api/terminals?")
                    })
                    .count()
            };
            let lists = inventory_reads();
            let pause = mock.pause_websocket_reads().await;
            let path = host.home.path().join("gterm-control.sock");
            let same = host.panes[0].host_id.clone();
            let cross = host.panes.last().unwrap().host_id.clone();
            let (capture_tx, mut capture_rx) = mpsc::channel(1);
            let captures = tokio::spawn(async move {
                let mut control = Control::connect(&path).await;
                for index in 0..120 {
                    capture_rx
                        .recv()
                        .await
                        .expect("one capture per measured key");
                    let target = if index % 2 == 0 { &same } else { &cross };
                    control
                        .rpc(
                            "snapshot",
                            json!({"host_terminal_id":target,
                        "max_lines":1000,"max_bytes":262144}),
                        )
                        .await;
                }
            });
            let probe = LivenessProbe::new(&mock, &input_tx, draws, frames.clone());
            let mut delivery = Vec::new();
            let mut echo = Vec::new();
            for index in 0..120 {
                if index == 40 {
                    mock.enqueue(
                        "GET",
                        "/api/terminals?",
                        200,
                        json!({
                            "items":items, "next_cursor":null,
                            "snapshot":{"daemon_epoch":"epoch-1","seq":2}
                        }),
                    );
                    pause.notify_one();
                    mock.close_websockets_service_restart();
                }
                let key = char::from(b'a' + (index % 26) as u8);
                capture_tx.send(()).await.unwrap();
                let started = std::time::Instant::now();
                probe.key(KeyCode::Char(key), KeyModifiers::NONE).await;
                let mut received = [0];
                timeout(
                    Duration::from_millis(150),
                    host.panes[0].receipt.read_exact(&mut received),
                )
                .await
                .expect("PTY receipt deadline")
                .unwrap();
                delivery.push(started.elapsed());
                assert_eq!(received, [key as u8], "exact ordered native input");
                let tag = format!("e{}{key}", index + 1);
                let rendered = timeout(Duration::from_millis(250), async {
                    loop {
                        if frames
                            .lock()
                            .unwrap()
                            .last()
                            .is_some_and(|frame| frame.contains(&tag))
                        {
                            break;
                        }
                        draw_notify.notified().await;
                    }
                })
                .await;
                if rendered.is_err() {
                    let snapshot = frames.lock().unwrap().last().cloned().unwrap_or_default();
                    panic!(
                        "echo deadline panes={count} sample={index} tag={tag}: {}",
                        snapshot.chars().take(4000).collect::<String>()
                    );
                }
                echo.push(started.elapsed());
            }
            captures.await.unwrap();
            let mut extra = [0];
            assert!(
                timeout(
                    Duration::from_millis(20),
                    host.panes[0].receipt.read(&mut extra)
                )
                .await
                .is_err(),
                "the PTY received an extra native keystroke"
            );
            wait_within(
                Duration::from_secs(15),
                "daemon reconnect and inventory reconciliation",
                || mock.websocket_handshakes() > handshakes && inventory_reads() > lists,
            )
            .await;
            assert!(written(&mock, &host.panes[0].id).is_empty());
            // A single identity and child survive daemon disconnect/reconciliation.
            let ping = host.control.rpc("ping", json!({})).await;
            assert_eq!(ping["host_epoch"], host.epoch);
            drop(input_tx);
            (delivery, echo)
        };
        let mut switch = TerminalGuard::recording().0;
        let (result, (mut delivery, mut echo)) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch
            ),
            driver
        );
        result.unwrap();
        delivery.sort();
        echo.sort();
        let (d99, e99) = (nearest_rank(&delivery, 99), nearest_rank(&echo, 99));
        eprintln!(
            "real PTY panes={count}: delivery p99={d99:?} max={:?}; echo p99={e99:?} max={:?}",
            delivery.last(),
            echo.last()
        );
        assert!(d99 <= Duration::from_millis(50));
        assert!(delivery.last().unwrap() <= &Duration::from_millis(150));
        assert!(e99 <= Duration::from_millis(100));
        assert!(echo.last().unwrap() <= &Duration::from_millis(250));
        drop(host);
        drop(workspace);
        timeout(Duration::from_secs(5), mock.shutdown())
            .await
            .expect("isolated daemon shutdown");
    }
}
