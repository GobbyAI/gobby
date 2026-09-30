//! A terminal host that speaks the frame protocol over a real Unix socket, the
//! way gterm does. Shared by the client-loop and host-recovery suites so a
//! direct pane has one fake host to attach, reconnect and refuse against.
//!
//! Connections are served concurrently, which is what makes the upgrade
//! window testable: a reconnect that lands while the previous stream is still
//! draining must not have to wait for it, and a connect made during the exec
//! waits in the listener backlog because the listener fd survives the exec.

#![allow(dead_code)]

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use gobby_client::daemon::LiveDaemon;
use gobby_client::frame_source::Transport;
use gobby_client::Workspace;
use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use serde_json::{json, Value};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{broadcast, watch};
use tokio::time::{timeout, Duration};

use super::MockDaemon;

/// How long a host wait may take before the test fails.
const WAIT: Duration = Duration::from_secs(10);

/// A host that answers every accepted connection with `Welcome { host_epoch }`,
/// records what the client sends after the handshake, and lets the test hold
/// the exec, move the advertised epoch, inject server messages, or drop a live
/// connection.
pub struct DirectHost {
    socket_dir: tempfile::TempDir,
    socket_path: std::path::PathBuf,
    host_epoch: Arc<Mutex<String>>,
    /// `false` holds every future connection before it answers, which is how a
    /// host mid-exec behaves; the connect itself already succeeded.
    accept_gate: Arc<watch::Sender<bool>>,
    /// Every client message after the handshake of every connection.
    seen: Arc<Mutex<Vec<ClientMessage>>>,
    /// Connections that reached `AttachTerminal`.
    attaches: Arc<AtomicUsize>,
    connections: Arc<AtomicUsize>,
    /// Server messages injected onto every live connection.
    pub to_client: broadcast::Sender<ServerMessage>,
    disconnect: broadcast::Sender<()>,
    /// While set, the next attach is answered with `ServerMessage::Error`
    /// instead of `Attached`, with the socket left open, so a caller can prove
    /// a definitive refusal is not mistaken for a restored stream (#23076).
    refuse_attach: Arc<watch::Sender<bool>>,
    /// While set, the next successful attach answers `Attached` naming this
    /// terminal instead of the one the client asked for, as a host that
    /// resolved the request to a different terminal does (#23076).
    relocate_attach: Arc<Mutex<Option<String>>>,
    task: tokio::task::JoinHandle<()>,
}

impl DirectHost {
    /// Listen on a fresh socket and answer attaches with `host_epoch`.
    pub async fn start(host_epoch: &str) -> Self {
        let socket_dir = tempfile::tempdir().expect("direct socket dir");
        let socket_path = socket_dir.path().join("frames.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind direct frame socket");
        let host_epoch = Arc::new(Mutex::new(host_epoch.to_string()));
        let (accept_gate, _) = watch::channel(true);
        let accept_gate = Arc::new(accept_gate);
        let seen: Arc<Mutex<Vec<ClientMessage>>> = Arc::new(Mutex::new(Vec::new()));
        let attaches = Arc::new(AtomicUsize::new(0));
        let connections = Arc::new(AtomicUsize::new(0));
        let (to_client, _) = broadcast::channel(64);
        let (disconnect, _) = broadcast::channel(8);
        let (refuse_attach, _) = watch::channel(false);
        let refuse_attach = Arc::new(refuse_attach);
        let relocate_attach: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
        let shared = Arc::new(HostShared {
            seen: Arc::clone(&seen),
            attaches: Arc::clone(&attaches),
            refuse_attach: Arc::clone(&refuse_attach),
            relocate_attach: Arc::clone(&relocate_attach),
        });
        let task = tokio::spawn({
            let host_epoch = Arc::clone(&host_epoch);
            let accept_gate = Arc::clone(&accept_gate);
            let connections = Arc::clone(&connections);
            let to_client = to_client.clone();
            let disconnect = disconnect.clone();
            let shared = Arc::clone(&shared);
            async move {
                loop {
                    let Ok((stream, _)) = listener.accept().await else {
                        return;
                    };
                    connections.fetch_add(1, Ordering::SeqCst);
                    // A host mid-exec does not answer: the connect lands in the
                    // listener backlog because the listener fd survives the
                    // exec, and nothing is read until the new image is up. The
                    // hold is checked after the accept so a connection that
                    // arrived while the gate was still open cannot slip a
                    // handshake through.
                    let mut gate = accept_gate.subscribe();
                    while !*gate.borrow_and_update() {
                        if gate.changed().await.is_err() {
                            return;
                        }
                    }
                    let epoch = host_epoch.lock().expect("host epoch").clone();
                    tokio::spawn(serve(
                        stream,
                        epoch,
                        Arc::clone(&shared),
                        to_client.subscribe(),
                        disconnect.subscribe(),
                    ));
                }
            }
        });
        Self {
            socket_dir,
            socket_path,
            host_epoch,
            accept_gate,
            seen,
            attaches,
            connections,
            to_client,
            disconnect,
            refuse_attach: refuse_attach.clone(),
            relocate_attach: relocate_attach.clone(),
            task,
        }
    }

    /// The host's socket path.
    pub fn socket_path(&self) -> &std::path::Path {
        &self.socket_path
    }

    /// Connections the host has accepted.
    pub fn connections(&self) -> usize {
        self.connections.load(Ordering::SeqCst)
    }

    /// Connections that reached `AttachTerminal`: one per accepted attach.
    pub fn attaches(&self) -> usize {
        self.attaches.load(Ordering::SeqCst)
    }

    /// Hold every future connection before it answers, as a host mid-exec does
    /// while its listener backlog absorbs the connect. Release with
    /// [`DirectHost::release_accepts`].
    pub fn hold_accepts(&self) -> Arc<watch::Sender<bool>> {
        // `send_replace`, not `send`: the receiver created in `start` is
        // dropped immediately, and `send` refuses (leaving the value true)
        // whenever there is no receiver. The hold must land regardless.
        let _ = self.accept_gate.send_replace(false);
        Arc::clone(&self.accept_gate)
    }

    /// Let held and future connections answer.
    pub fn release_accepts(&self, gate: &watch::Sender<bool>) {
        let _ = gate.send_replace(true);
    }

    /// Move the host's advertised epoch, as a new host image does.
    pub fn set_host_epoch(&self, epoch: &str) {
        *self.host_epoch.lock().expect("host epoch") = epoch.to_string();
    }

    /// Answer exactly the next attach with a `terminal_gone` refusal, leaving
    /// the socket open, as a host whose named terminal disappeared does.
    pub fn refuse_next_attach(&self) {
        self.refuse_attach.send_replace(true);
    }

    /// Answer exactly the next successful attach with `Attached` naming
    /// `host_terminal_id` rather than the terminal the client asked for, as a
    /// host that resolved the pane to a different terminal does (#23076).
    pub fn answer_next_attach_with_terminal_id(&self, host_terminal_id: &str) {
        *self.relocate_attach.lock().expect("relocate attach") = Some(host_terminal_id.to_string());
    }

    /// The roster `attach` block that tells gclient this terminal has a host
    /// socket, so the attach asks for direct frames before proxy frames.
    pub fn roster_attach(&self, terminal_id: &str) -> Value {
        json!({
            "backend": "native",
            "frame_host_epoch": self.host_epoch.lock().expect("host epoch").clone(),
            "host_socket": self.socket_path.to_string_lossy(),
            "host_terminal_id": terminal_id,
        })
    }

    /// The `direct` locator the daemon returns with a direct attach result.
    pub fn attach_locator(&self, terminal_id: &str) -> Value {
        json!({
            "host_epoch": self.host_epoch.lock().expect("host epoch").clone(),
            "host_terminal_id": terminal_id,
            "frame_socket_path": self.socket_path.to_string_lossy(),
            "pane": null,
        })
    }

    /// Every client message the host has received so far, without waiting.
    pub fn drain(&self) -> Vec<ClientMessage> {
        self.seen.lock().expect("host messages").clone()
    }

    /// Wait for the host to have answered `expected` attaches.
    pub async fn expect_attaches(&self, expected: usize) {
        self.wait_until("an attach", |host| host.attaches() >= expected)
            .await;
    }

    /// Wait for `expected` input verbs (`BindAttachment`, `Input` or `Paste`).
    pub async fn expect_input(&self, expected: usize) {
        self.wait_until("input", |host| {
            host.drain()
                .iter()
                .filter(|message| {
                    matches!(
                        message,
                        ClientMessage::BindAttachment { .. }
                            | ClientMessage::Input { .. }
                            | ClientMessage::Paste { .. }
                    )
                })
                .count()
                >= expected
        })
        .await;
    }

    /// Wait for the host to have seen a batch `predicate` accepts.
    pub async fn wait_for(
        &self,
        what: &str,
        mut predicate: impl FnMut(&[ClientMessage]) -> bool,
    ) -> Vec<ClientMessage> {
        self.wait_until(what, move |host| predicate(&host.drain()))
            .await;
        self.drain()
    }

    async fn wait_until(&self, what: &str, mut predicate: impl FnMut(&Self) -> bool) {
        timeout(WAIT, async {
            loop {
                if predicate(self) {
                    return;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("the host never received {what}"));
    }

    /// Drop every live connection; each client's reader reports EOF.
    pub fn disconnect(&self) {
        let _ = self.disconnect.send(());
    }

    /// Abort the host's accept loop and its socket.
    pub async fn shutdown(self) {
        self.task.abort();
        let _ = self.task.await;
        drop(self.socket_dir);
    }
}

/// A live workspace whose single native pane is attached over `host`'s real
/// frame socket, so `Pane::transport()` is `Direct` and the pane types on that
/// socket instead of the daemon (#22573). Keep the returned home alive.
pub async fn live_workspace_on_direct_host(
    mock: &MockDaemon,
    host: &DirectHost,
    terminal_id: &str,
) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    let home = tempfile::tempdir().expect("gobby home");
    std::fs::write(
        home.path()
            .join(gobby_core::local_token::LOCAL_CLI_TOKEN_FILENAME),
        "local-token\n",
    )
    .expect("write local cli token");
    mock.serve_direct_attach(host.attach_locator(terminal_id));
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{
                    "terminal_id": terminal_id,
                    "backend": "native",
                    "state": "live",
                    "attach": host.roster_attach(terminal_id),
                }],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install the direct attachment");
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    assert_eq!(
        workspace.pane(pane_id).transport(),
        Some(Transport::Direct),
        "the roster advertised a host socket, so the attach must be direct"
    );
    (workspace, home)
}

struct HostShared {
    seen: Arc<Mutex<Vec<ClientMessage>>>,
    attaches: Arc<AtomicUsize>,
    refuse_attach: Arc<watch::Sender<bool>>,
    relocate_attach: Arc<Mutex<Option<String>>>,
}

async fn serve(
    mut stream: UnixStream,
    epoch: String,
    shared: Arc<HostShared>,
    mut to_client: broadcast::Receiver<ServerMessage>,
    mut disconnect: broadcast::Receiver<()>,
) {
    let seen = &shared.seen;
    let attaches = &shared.attaches;
    let refuse_attach = &shared.refuse_attach;
    let relocate_attach = &shared.relocate_attach;
    let _: ClientMessage = match read_message_async(&mut stream, MAX_FRAME_SIZE).await {
        Ok(message) => message,
        Err(_) => return,
    };
    if write_message_async(&mut stream, &ServerMessage::Welcome { host_epoch: epoch })
        .await
        .is_err()
    {
        return;
    }
    let attach: ClientMessage = match read_message_async(&mut stream, MAX_FRAME_SIZE).await {
        Ok(message) => message,
        Err(_) => return,
    };
    let host_terminal_id = match &attach {
        ClientMessage::AttachTerminal {
            host_terminal_id, ..
        } => host_terminal_id.clone(),
        _ => String::new(),
    };
    seen.lock().expect("host messages").push(attach);
    // A host that has no such terminal, or is at capacity, refuses the attach
    // while keeping the socket open. The client must not treat that as a
    // restored stream (#23076).
    if *refuse_attach.borrow() {
        refuse_attach.send_replace(false);
        let _ = write_message_async(
            &mut stream,
            &ServerMessage::Error {
                code: "terminal_gone".into(),
                message: None,
            },
        )
        .await;
        // Keep the socket open, as the real host does for a refusal: prove the
        // client falls back on the reply, not on an end of stream.
        loop {
            match read_message_async::<_, ClientMessage>(&mut stream, MAX_FRAME_SIZE).await {
                Ok(_) => {}
                Err(_) => return,
            }
        }
    }
    attaches.fetch_add(1, Ordering::SeqCst);
    let answered_terminal_id = relocate_attach
        .lock()
        .expect("relocate attach")
        .take()
        .unwrap_or(host_terminal_id);
    if write_message_async(
        &mut stream,
        &ServerMessage::Attached {
            created: false,
            host_terminal_id: answered_terminal_id,
        },
    )
    .await
    .is_err()
    {
        return;
    }
    loop {
        tokio::select! {
            message = read_message_async(&mut stream, MAX_FRAME_SIZE) => {
                match message {
                    Ok(message) => {
                        seen.lock().expect("host messages").push(message);
                    }
                    Err(_) => return,
                }
            }
            outgoing = to_client.recv() => {
                match outgoing {
                    Ok(outgoing) => {
                        if write_message_async(&mut stream, &outgoing).await.is_err() {
                            return;
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(_)) => {}
                    Err(broadcast::error::RecvError::Closed) => return,
                }
            }
            disconnect = disconnect.recv() => {
                match disconnect {
                    Ok(()) | Err(broadcast::error::RecvError::Closed) => return,
                    Err(broadcast::error::RecvError::Lagged(_)) => return,
                }
            }
        }
    }
}
