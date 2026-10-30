//! Isolated terminal-host protocol double; never contacts the installed host.
use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::Path,
    process::{Child, Command},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::UnixListener,
    sync::{Notify, broadcast},
    task::{JoinHandle, JoinSet},
};

pub const TOKEN: &str = "node-host-fixture-token";
pub const EPOCH: &str = "node-host-fixture-epoch";

pub struct FakeGterm {
    process: Child,
    task: JoinHandle<()>,
    requests: Arc<Mutex<Vec<Value>>>,
    drop_next_list: Arc<AtomicBool>,
    reject_next_commit: Arc<AtomicBool>,
    events: broadcast::Sender<Value>,
    exit_before_commit: Arc<AtomicBool>,
    disconnect_after_exit: Arc<AtomicBool>,
    resume_commit: Arc<Notify>,
    pause_list: Arc<AtomicBool>,
    list_snapshot: Arc<Notify>,
    resume_list: Arc<Notify>,
}

impl FakeGterm {
    pub async fn start(home: &Path) -> Result<Self> {
        Self::start_with_inventory(home, Vec::new()).await
    }

    pub async fn start_with_inventory(home: &Path, inventory: Vec<Value>) -> Result<Self> {
        let binary = home.join("bin/gterm");
        std::fs::create_dir_all(binary.parent().context("binary parent")?)?;
        std::fs::copy("/bin/sleep", &binary)?;
        #[cfg(target_os = "macos")]
        anyhow::ensure!(
            Command::new("/usr/bin/codesign")
                .args(["--force", "--sign", "-"])
                .arg(&binary)
                .status()?
                .success(),
            "sign fixture executable"
        );
        let mut process = Command::new(&binary).arg("600").spawn()?;
        let setup = (|| -> Result<UnixListener> {
            std::fs::write(home.join("gterm.pid"), process.id().to_string())?;
            std::fs::write(home.join("gterm-control.token"), format!("{TOKEN}\n"))?;
            Ok(UnixListener::bind(home.join("gterm-control.sock"))?)
        })();
        let listener = match setup {
            Ok(listener) => listener,
            Err(error) => {
                let _ = process.kill();
                let _ = process.wait();
                return Err(error);
            }
        };
        let requests = Arc::new(Mutex::new(Vec::new()));
        let drop_next_list = Arc::new(AtomicBool::new(false));
        let reject_next_commit = Arc::new(AtomicBool::new(false));
        let (events, _) = broadcast::channel(16);
        let exit_before_commit = Arc::new(AtomicBool::new(false));
        let disconnect_after_exit = Arc::new(AtomicBool::new(false));
        let resume_commit = Arc::new(Notify::new());
        let pause_list = Arc::new(AtomicBool::new(false));
        let list_snapshot = Arc::new(Notify::new());
        let resume_list = Arc::new(Notify::new());
        let script = Arc::new(Mutex::new(HostScript {
            inventory,
            pending: BTreeMap::new(),
            seq: 0,
        }));
        let task = tokio::spawn({
            let requests = requests.clone();
            let drop_next_list = drop_next_list.clone();
            let reject_next_commit = reject_next_commit.clone();
            let events = events.clone();
            let exit_before_commit = exit_before_commit.clone();
            let disconnect_after_exit = disconnect_after_exit.clone();
            let resume_commit = resume_commit.clone();
            let pause_list = pause_list.clone();
            let list_snapshot = list_snapshot.clone();
            let resume_list = resume_list.clone();
            let pid = process.id();
            async move {
                let mut connections = JoinSet::new();
                loop {
                    tokio::select! {
                        accepted = listener.accept() => {
                            let Ok((stream, _)) = accepted else { return };
                            let requests = requests.clone();
                            let drop_next_list = drop_next_list.clone();
                            let reject_next_commit = reject_next_commit.clone();
                            let emit_events = events.clone();
                            let mut events = events.subscribe();
                            let exit_before_commit = exit_before_commit.clone();
                            let disconnect_after_exit = disconnect_after_exit.clone();
                            let resume_commit = resume_commit.clone();
                            let pause_list = pause_list.clone();
                            let list_snapshot = list_snapshot.clone();
                            let resume_list = resume_list.clone();
                            let script = script.clone();
                            connections.spawn(async move {
                                let (reader, mut writer) = stream.into_split();
                                let mut reader = BufReader::new(reader);
                                let mut authenticated = false;
                                let mut subscribed = false;
                                let mut line = String::new();
                                loop {
                                    line.clear();
                                    let request = tokio::select! {
                                        read = reader.read_line(&mut line) => {
                                            if !matches!(read, Ok(n) if n > 0) { return }
                                            let Ok(request) = serde_json::from_str::<Value>(&line) else { return };
                                            request
                                        }
                                        event = events.recv(), if subscribed => {
                                            let Ok(event) = event else { return };
                                            if send(&mut writer, &event).await.is_err() { return }
                                            if event["event"] == "terminal_exited"
                                                && disconnect_after_exit.swap(false, Ordering::SeqCst)
                                            { return }
                                            continue;
                                        }
                                    };
                                    requests.lock().expect("fixture requests").push(request.clone());
                                    let method = request["method"].as_str().unwrap_or("");
                                    if method == "list" && drop_next_list.swap(false, Ordering::SeqCst) { return }
                                    let mut reply = if method == "hello" {
                                        authenticated = request["control_token"] == TOKEN;
                                        if authenticated {
                                            json!({"ok":true,"protocol_version":1,"host_epoch":EPOCH,
                                                "version":"fixture","capabilities":[]})
                                        } else { json!({"ok":false,"error":"token_mismatch"}) }
                                    } else if !authenticated {
                                        json!({"ok":false,"error":"authentication_failure"})
                                    } else if method == "spawn_commit" && reject_next_commit.swap(false, Ordering::SeqCst) {
                                        json!({"ok":false,"error":"spawn_key_mismatch"})
                                    } else {
                                        subscribed |= method == "subscribe_events";
                                        script.lock().expect("fixture inventory").reply(&request, pid)
                                    };
                                    if method == "list" && pause_list.swap(false, Ordering::SeqCst) {
                                        list_snapshot.notify_one();
                                        resume_list.notified().await;
                                    }
                                    // Keep the event cursor contiguous with this fixture's
                                    // spawn/commit sequence increments before emitting the exit.
                                    if matches!(method, "spawn" | "spawn_commit")
                                        && exit_before_commit.load(Ordering::SeqCst)
                                    {
                                        let _ = emit_events.send(json!({"event":"input_activity",
                                            "epoch":EPOCH,"seq":reply["seq"],
                                            "terminal_id":request["terminal_id"],
                                            "host_terminal_id":format!("host-{}", request["terminal_id"].as_str().unwrap()),
                                            "attachment_id":"fixture-input","kind":"input",
                                            "bytes":1,"submit":false}));
                                    }
                                    if method == "spawn_commit" && reply["ok"] == true
                                        && exit_before_commit.swap(false, Ordering::SeqCst)
                                    {
                                        let event = {
                                            let mut script = script.lock().expect("fixture inventory");
                                            script.inventory.retain(|item| item["terminal_id"] != request["terminal_id"]);
                                            script.seq += 1;
                                            json!({"event":"terminal_exited","epoch":EPOCH,"seq":script.seq,
                                                "terminal_id":request["terminal_id"],
                                                "host_terminal_id":format!("host-{}", request["terminal_id"].as_str().unwrap()),
                                                "exit_code":7})
                                        };
                                        let _ = emit_events.send(event);
                                        resume_commit.notified().await;
                                    }
                                    reply["id"] = request["id"].clone();
                                    if send(&mut writer, &reply).await.is_err() { return }
                                }
                            });
                        }
                        _ = connections.join_next(), if !connections.is_empty() => {}
                    }
                }
            }
        });
        Ok(Self {
            process,
            task,
            requests,
            drop_next_list,
            reject_next_commit,
            events,
            exit_before_commit,
            disconnect_after_exit,
            resume_commit,
            pause_list,
            list_snapshot,
            resume_list,
        })
    }

    pub fn pause_next_list_reply(&self) {
        self.pause_list.store(true, Ordering::SeqCst);
    }

    pub async fn wait_for_list_snapshot(&self) {
        self.list_snapshot.notified().await;
    }

    pub fn release_list_reply(&self) {
        self.resume_list.notify_one();
    }

    pub fn requests(&self) -> Vec<Value> {
        self.requests.lock().expect("fixture requests").clone()
    }

    pub fn drop_next_list(&self) {
        self.drop_next_list.store(true, Ordering::SeqCst);
    }

    pub fn reject_next_commit(&self) {
        self.reject_next_commit.store(true, Ordering::SeqCst);
    }

    pub fn terminal_exited(&self) -> Result<()> {
        self.events.send(json!({"event":"terminal_exited","epoch":EPOCH,"seq":1,
            "terminal_id":"fixture-terminal","host_terminal_id":"fixture-host-terminal","exit_code":0}))?;
        Ok(())
    }

    /// Hold the commit reply until the test observes the exit watcher's reconnect.
    pub fn exit_before_next_commit_reply(&self) {
        self.exit_before_commit.store(true, Ordering::SeqCst);
        self.disconnect_after_exit.store(true, Ordering::SeqCst);
    }

    pub fn release_commit_reply(&self) {
        self.resume_commit.notify_one();
    }

    pub fn alive(&mut self) -> Result<bool> {
        Ok(self.process.try_wait()?.is_none())
    }
}

struct HostScript {
    inventory: Vec<Value>,
    pending: BTreeMap<String, Value>,
    seq: u64,
}

impl HostScript {
    fn reply(&mut self, request: &Value, pid: u32) -> Value {
        match request["method"].as_str().unwrap_or("") {
            "ping" => json!({"ok":true,"host_epoch":EPOCH,"version":"fixture","host_pid":pid}),
            "list" => json!({"ok":true,"epoch":EPOCH,"seq":self.seq,"terminals":self.inventory}),
            "subscribe_events" => json!({"ok":true,"epoch":EPOCH,"seq":self.seq,"gap":false}),
            "spawn" => {
                let terminal_id = request["terminal_id"].as_str().unwrap_or("invalid");
                let prepared = json!({"terminal_id":terminal_id,
                    "host_terminal_id":format!("host-{terminal_id}"), "spawn_key":request["spawn_key"],
                    "pgid":pid, "start_time":1.0});
                self.pending
                    .insert(terminal_id.to_owned(), prepared.clone());
                self.seq += 1;
                let mut reply = prepared;
                reply["ok"] = json!(true);
                reply["epoch"] = json!(EPOCH);
                reply["seq"] = json!(self.seq);
                reply
            }
            "spawn_commit" => {
                let id = request["terminal_id"].as_str().unwrap_or("invalid");
                match self.pending.get(id) {
                    Some(prepared) if prepared["spawn_key"] == request["spawn_key"] => {
                        self.inventory
                            .push(self.pending.remove(id).expect("prepared entry"));
                        self.ack()
                    }
                    _ => json!({"ok":false,"error":"spawn_key_mismatch"}),
                }
            }
            "kill" => {
                self.inventory
                    .retain(|item| item["host_terminal_id"] != request["host_terminal_id"]);
                self.pending
                    .retain(|_, item| item["host_terminal_id"] != request["host_terminal_id"]);
                self.ack()
            }
            "write" => self.ack(),
            _ => json!({"ok":false,"error":"unknown_method"}),
        }
    }

    fn ack(&mut self) -> Value {
        self.seq += 1;
        json!({"ok":true,"epoch":EPOCH,"seq":self.seq})
    }
}

async fn send(writer: &mut tokio::net::unix::OwnedWriteHalf, reply: &Value) -> Result<()> {
    let mut bytes = serde_json::to_vec(reply)?;
    bytes.push(b'\n');
    writer.write_all(&bytes).await?;
    Ok(())
}

impl Drop for FakeGterm {
    fn drop(&mut self) {
        self.task.abort();
        let _ = self.process.kill();
        let _ = self.process.wait();
    }
}
