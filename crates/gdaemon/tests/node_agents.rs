#![cfg(unix)]

#[path = "node_support/fake_gterm.rs"]
mod fake_gterm;

use anyhow::Result;
use gobby_core::bootstrap::parse_hub_database_bootstrap;
use gobby_daemon::node_commands::{ExitReason, ReportExit, WorkspaceMode};
use gobby_daemon::nodes::agents::{
    NodeAgents,
    state::{AgentState, ExitRecord, RunPhase, RunRecord, WorkspaceRecord},
};
use gobby_daemon::state::NodeServices;
use std::{fs, os::unix::fs::MetadataExt, os::unix::fs::PermissionsExt, path::Path};

#[tokio::test]
async fn host_epoch_counter_follows_epoch() -> Result<()> {
    use gobby_daemon::nodes::agents::host::NodeEpochAuthority;
    use gobby_terminals::host::EpochAuthority;
    let home = tempfile::tempdir()?;
    let agents = std::sync::Arc::new(NodeAgents::open(home.path())?);
    let authority = NodeEpochAuthority {
        agents: agents.clone(),
    };
    let machine = uuid::Uuid::new_v4();
    authority.observe_epoch(machine, "epoch-one").await?;
    authority.observe_epoch(machine, "epoch-one").await?;
    assert_eq!(agents.state.lock().await.host_epoch, 1);
    let reopened = NodeAgents::open(home.path())?;
    assert_eq!(
        reopened.state.lock().await.host_epoch_id.as_deref(),
        Some("epoch-one")
    );
    authority.observe_epoch(machine, "epoch-two").await?;
    assert_eq!(agents.state.lock().await.host_epoch, 2);
    let disk: AgentState =
        serde_json::from_slice(&fs::read(home.path().join("node/agents.json"))?)?;
    assert_eq!(disk.host_epoch, 2);
    assert_eq!(disk.host_epoch_id.as_deref(), Some("epoch-two"));
    assert!(authority.bindings("epoch-two").await?.is_empty());
    Ok(())
}

#[tokio::test]
async fn host_control_reconnects() -> Result<()> {
    let home = tempfile::tempdir()?;
    let host = fake_gterm::FakeGterm::start(home.path()).await?;
    let agents = NodeAgents::open(home.path())?;
    let first = agents.host.call(|client| Box::pin(client.list())).await?;
    assert_eq!(first.epoch, fake_gterm::EPOCH);
    host.drop_next_list();
    let second = agents.host.call(|client| Box::pin(client.list())).await?;
    assert_eq!(second.epoch, fake_gterm::EPOCH);
    let requests = host.requests();
    assert_eq!(
        requests.iter().filter(|r| r["method"] == "hello").count(),
        2
    );
    assert_eq!(requests.iter().filter(|r| r["method"] == "list").count(), 3);
    assert!(
        requests
            .iter()
            .filter(|r| r["method"] == "hello")
            .all(|r| r["control_token"] == fake_gterm::TOKEN)
    );
    Ok(())
}

#[tokio::test]
async fn node_adopts_terminal_host() -> Result<()> {
    use std::{net::TcpListener, time::Duration};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let home = tempfile::tempdir()?;
    let mut host = fake_gterm::FakeGterm::start(home.path()).await?;
    let first = TcpListener::bind("127.0.0.1:0")?;
    let second = TcpListener::bind("127.0.0.1:0")?;
    let port = first.local_addr()?.port();
    fs::write(
        home.path().join("bootstrap.yaml"),
        format!(
            "datastore_mode: remote\nhub_daemon_url: http://127.0.0.1:1\napi_key: node-test-key\n\
         daemon_port: {port}\nwebsocket_port: {}\nbind_host: 127.0.0.1\n",
            second.local_addr()?.port()
        ),
    )?;
    fs::write(
        home.path().join("machine_id"),
        uuid::Uuid::new_v4().to_string(),
    )?;
    drop((first, second));
    let log = home.path().join("serve.log");
    let mut child = tokio::process::Command::new(env!("CARGO_BIN_EXE_gdaemon"))
        .kill_on_drop(true)
        .arg("serve")
        .env("GOBBY_HOME", home.path())
        .env_remove("GOBBY_PARENT_FD")
        .env_remove("GOBBY_SERVICE_LAUNCH")
        .env_remove("GOBBY_SERVICE_NONCE")
        .stderr(fs::File::create(&log)?)
        .spawn()?;
    let mut stream = tokio::time::timeout(Duration::from_secs(8), async {
        loop {
            anyhow::ensure!(
                child.try_wait()?.is_none(),
                "node exited: {}",
                fs::read_to_string(&log)?
            );
            if let Ok(stream) = tokio::net::TcpStream::connect(("127.0.0.1", port)).await {
                return Ok::<_, anyhow::Error>(stream);
            }
            tokio::task::yield_now().await;
        }
    })
    .await??;
    // A bound socket alone precedes serve_doors polling shutdown_signal.
    stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await?;
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(5), stream.read_to_end(&mut response)).await??;
    assert!(
        response.starts_with(b"HTTP/1.1 503"),
        "unavailable fake hub must return HTTP 503"
    );
    let requests = host.requests();
    assert!(
        requests
            .iter()
            .any(|r| r["method"] == "hello" && r["control_token"] == fake_gterm::TOKEN),
        "node must authenticate to the adopted host; requests={requests:?}"
    );
    let state: AgentState =
        serde_json::from_slice(&fs::read(home.path().join("node/agents.json"))?)?;
    assert_eq!(state.host_epoch_id.as_deref(), Some(fake_gterm::EPOCH));
    // Graceful daemon shutdown must preserve the separately owned terminal host.
    // SAFETY: this is our live child, retained until wait reaps it below.
    assert_eq!(
        unsafe { libc::kill(child.id().expect("node PID") as i32, libc::SIGTERM) },
        0
    );
    let status = tokio::time::timeout(Duration::from_secs(5), child.wait()).await??;
    assert!(
        status.success(),
        "graceful node shutdown: {status}; {}",
        fs::read_to_string(&log)?
    );
    assert!(host.alive()?);
    Ok(())
}

#[tokio::test]
async fn fake_host_emits_terminal_exit() -> Result<()> {
    use gobby_terminals::control::{ControlClient, HostEventKind};
    let home = tempfile::tempdir()?;
    let host = fake_gterm::FakeGterm::start(home.path()).await?;
    let stream = tokio::net::UnixStream::connect(home.path().join("gterm-control.sock")).await?;
    let mut client = ControlClient::new(stream);
    client.hello(fake_gterm::TOKEN).await?;
    client.subscribe_events(None).await?;
    host.terminal_exited()?;
    let event =
        tokio::time::timeout(std::time::Duration::from_secs(2), client.next_event()).await??;
    assert!(matches!(event.kind, HostEventKind::TerminalExited { .. }));
    Ok(())
}

#[tokio::test]
async fn host_control_rejects_wrong_token_without_replay() -> Result<()> {
    use gobby_terminals::control::ControlError;
    let home = tempfile::tempdir()?;
    let host = fake_gterm::FakeGterm::start(home.path()).await?;
    fs::write(home.path().join("gterm-control.token"), "wrong-token\n")?;
    let agents = NodeAgents::open(home.path())?;
    let result = agents.host.call(|client| Box::pin(client.list())).await;
    assert!(matches!(result, Err(ControlError::Host { error, .. }) if error == "token_mismatch"));
    let requests = host.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["method"], "hello");
    Ok(())
}

#[tokio::test]
async fn host_epoch_commit_failure_does_not_publish_generation() -> Result<()> {
    use gobby_daemon::nodes::agents::host::NodeEpochAuthority;
    use gobby_terminals::host::EpochAuthority;
    let home = tempfile::tempdir()?;
    let agents = std::sync::Arc::new(NodeAgents::open(home.path())?);
    let authority = NodeEpochAuthority {
        agents: agents.clone(),
    };
    let machine = uuid::Uuid::new_v4();
    authority.observe_epoch(machine, "old").await?;
    let saved = agents.state.lock().await.clone();
    fs::rename(home.path().join("node"), home.path().join("saved-node"))?;
    fs::write(home.path().join("node"), b"not a directory")?;
    assert!(authority.observe_epoch(machine, "new").await.is_err());
    assert_eq!(*agents.state.lock().await, saved);
    fs::remove_file(home.path().join("node"))?;
    fs::rename(home.path().join("saved-node"), home.path().join("node"))?;
    authority.observe_epoch(machine, "new").await?;
    assert_eq!(agents.state.lock().await.host_epoch, 2);
    Ok(())
}

#[tokio::test]
async fn fake_host_scripts_inventory_and_spawn_lifecycle() -> Result<()> {
    use gobby_terminals::control::{ControlClient, SpawnRequest, WriteKind, WriteRequest};
    use serde_json::json;
    let home = tempfile::tempdir()?;
    let inventory = vec![json!({"terminal_id":"existing", "host_terminal_id":"existing-host"})];
    let host = fake_gterm::FakeGterm::start_with_inventory(home.path(), inventory.clone()).await?;
    let stream = tokio::net::UnixStream::connect(home.path().join("gterm-control.sock")).await?;
    let mut client = ControlClient::new(stream);
    client.hello(fake_gterm::TOKEN).await?;
    assert_eq!(client.list().await?.terminals, inventory);
    let request = SpawnRequest {
        terminal_id: "new".into(),
        spawn_key: "stable-key".into(),
        argv: vec!["fake-provider".into()],
        cwd: home.path().display().to_string(),
        env: Default::default(),
        cols: 80,
        rows: 24,
        commit_deadline_ms: 10_000,
        reservation_id: None,
        reserve_key: None,
    };
    let prepared = client.spawn(&request).await?;
    assert_eq!(prepared.terminal_id, "new");
    assert_eq!(prepared.spawn_key, "stable-key");
    assert_eq!(
        client.list().await?.terminals.len(),
        1,
        "prepare is not committed"
    );
    client
        .spawn_commit(&prepared.terminal_id, &prepared.spawn_key, None)
        .await?;
    let listed = client.list().await?;
    assert_eq!(listed.terminals.len(), 2);
    assert!(
        listed
            .terminals
            .iter()
            .any(|item| item["host_terminal_id"] == prepared.host_terminal_id)
    );
    client
        .write(&WriteRequest {
            host_terminal_id: prepared.host_terminal_id.clone(),
            kind: WriteKind::Input,
            data: b"hello".to_vec(),
            submit: Some(true),
        })
        .await?;
    client.kill(&prepared.host_terminal_id, 0).await?;
    assert_eq!(client.list().await?.terminals, inventory);
    let aborted = client
        .spawn(&SpawnRequest {
            terminal_id: "aborted".into(),
            spawn_key: "abort-key".into(),
            ..request
        })
        .await?;
    client.spawn_abort(&aborted, 0).await?;
    assert_eq!(client.list().await?.terminals, inventory);
    let requests = host.requests();
    for method in ["hello", "list", "spawn", "spawn_commit", "write", "kill"] {
        assert!(
            requests.iter().any(|r| r["method"] == method),
            "missing {method}"
        );
    }
    Ok(())
}

fn workspace(root: &Path, reused: bool) -> WorkspaceRecord {
    WorkspaceRecord {
        mode: WorkspaceMode::Worktree,
        path: root.join("workspace"),
        branch_name: "agent/run".into(),
        base_branch: "main".into(),
        source_root: root.join("source"),
        reused,
    }
}

fn run(phase: RunPhase, workspace: Option<WorkspaceRecord>) -> RunRecord {
    RunRecord {
        terminal_id: "terminal".into(),
        phase,
        accepted_at_ms: 100,
        workspace,
        host_terminal_id: Some("host-terminal".into()),
    }
}

fn exit(run_id: &str, ended_at_ms: u64) -> ExitRecord {
    ExitRecord {
        exit: ReportExit {
            run_id: run_id.into(),
            terminal_id: "finished-terminal".into(),
            launched: true,
            exit_code: Some(0),
            reason: ExitReason::Exited,
            workspace: None,
            sandbox: None,
        },
        ended_at_ms,
    }
}

#[tokio::test]
async fn agent_state_round_trips() -> Result<()> {
    let home = tempfile::tempdir()?;
    let agents = NodeAgents::open(home.path())?;
    let path = home.path().join("node/agents.json");
    assert!(path.is_file(), "opening absent state must persist it");
    assert!(
        tokio::time::timeout(std::time::Duration::from_secs(1), agents.changed.notified())
            .await
            .is_ok(),
        "initial commit must notify consumers"
    );
    assert_eq!(
        fs::metadata(path.parent().unwrap())?.permissions().mode() & 0o777,
        0o700
    );
    let old_file = fs::File::open(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644))?;
    let expected = {
        let mut state = agents.state.lock().await;
        state.hub_epoch = Some(7);
        state.host_epoch = 3;
        state.host_epoch_id = Some("host-epoch".into());
        state.runs.insert(
            "live".into(),
            run(RunPhase::Launched, Some(workspace(home.path(), false))),
        );
        state
            .removals
            .insert("old".into(), workspace(home.path(), false));
        state.cancelled.insert("cancelled".into(), 123);
        state.exits.push(exit("finished", 200));
        assert_eq!(state.live_and_accepted(), 1);
        state.commit(&agents.home, &agents.changed)?;
        state.clone()
    };
    assert_ne!(
        old_file.metadata()?.ino(),
        fs::metadata(&path)?.ino(),
        "commit must replace by rename"
    );
    assert_eq!(fs::metadata(&path)?.permissions().mode() & 0o777, 0o600);
    assert!(!home.path().join("node/agents.json.tmp").exists());
    assert!(
        tokio::time::timeout(std::time::Duration::from_secs(1), agents.changed.notified())
            .await
            .is_ok()
    );
    drop(agents);
    let reopened = NodeAgents::open(home.path())?;
    assert_eq!(*reopened.state.lock().await, expected);
    Ok(())
}

#[tokio::test]
async fn revision_never_repeats_after_restart() -> Result<()> {
    let home = tempfile::tempdir()?;
    let mut previous = 0;
    for _ in 0..4 {
        let agents = NodeAgents::open(home.path())?;
        let mut state = agents.state.lock().await;
        let revision = state.next_revision(&agents.home, &agents.changed)?;
        assert!(revision > previous, "revision must survive every reopen");
        let persisted: AgentState =
            serde_json::from_slice(&fs::read(home.path().join("node/agents.json"))?)?;
        assert_eq!(
            persisted.revision, revision,
            "value must be durable before return"
        );
        previous = revision;
    }
    Ok(())
}

#[test]
fn prepare_refuses_unreadable_agent_state() -> Result<()> {
    let home = tempfile::tempdir()?;
    let state_path = home.path().join("node/agents.json");
    fs::create_dir(state_path.parent().unwrap())?;
    fs::write(&state_path, b"{broken")?;
    let bootstrap = parse_hub_database_bootstrap(
        "datastore_mode: remote\nhub_daemon_url: http://127.0.0.1:1\napi_key: gobby_00000000000000000000000000000000000000000002CZclj\n",
    )?.expect("node bootstrap");
    let prepared =
        NodeServices::prepare(&bootstrap, &home.path().join("bootstrap.yaml"), home.path());
    assert!(prepared.is_err(), "corrupt state must prevent node startup");
    let message = format!("{:#}", prepared.err().unwrap());
    assert!(
        message.contains(&state_path.display().to_string()),
        "{message}"
    );
    assert!(message.contains("move it aside to reset"), "{message}");
    assert_eq!(fs::read(&state_path)?, b"{broken");
    Ok(())
}

#[tokio::test]
async fn cancel_records_survive_restart_and_expire() -> Result<()> {
    let home = tempfile::tempdir()?;
    let now = 2 * 24 * 60 * 60 * 1_000;
    let cutoff = now - 24 * 60 * 60 * 1_000;
    {
        let agents = NodeAgents::open(home.path())?;
        let mut state = agents.state.lock().await;
        state.cancelled.insert("expired".into(), cutoff - 1);
        state.cancelled.insert("boundary".into(), cutoff);
        state.cancelled.insert("recent".into(), now);
        state.cancelled.insert("future".into(), now + 1);
        state.commit(&agents.home, &agents.changed)?;
    }
    let agents = NodeAgents::open(home.path())?;
    {
        let mut state = agents.state.lock().await;
        assert_eq!(
            state.cancelled.len(),
            4,
            "open must retain cancellation records"
        );
        state.prune_cancelled(now);
        assert!(!state.cancelled.contains_key("expired"));
        assert!(state.cancelled.contains_key("boundary"));
        assert!(state.cancelled.contains_key("recent"));
        assert!(state.cancelled.contains_key("future"));
        state.commit(&agents.home, &agents.changed)?;
    }
    let reopened = NodeAgents::open(home.path())?;
    assert_eq!(reopened.state.lock().await.cancelled.len(), 3);
    Ok(())
}

#[tokio::test]
async fn restart_fails_unlaunched_runs() -> Result<()> {
    let home = tempfile::tempdir()?;
    {
        let agents = NodeAgents::open(home.path())?;
        let mut state = agents.state.lock().await;
        state.exits.push(exit("previous", 1));
        state
            .removals
            .insert("existing-removal".into(), workspace(home.path(), false));
        state
            .runs
            .insert("accepted".into(), run(RunPhase::Accepted, None));
        state.runs.insert(
            "preparing".into(),
            run(RunPhase::Preparing, Some(workspace(home.path(), false))),
        );
        state.runs.insert(
            "reused".into(),
            run(RunPhase::Preparing, Some(workspace(home.path(), true))),
        );
        state.runs.insert(
            "launched".into(),
            run(RunPhase::Launched, Some(workspace(home.path(), false))),
        );
        assert_eq!(state.live_and_accepted(), 4);
        state.commit(&agents.home, &agents.changed)?;
    }
    let recovered = {
        let agents = NodeAgents::open(home.path())?;
        let state = agents.state.lock().await;
        assert_eq!(state.live_and_accepted(), 1);
        assert!(state.runs.contains_key("launched"));
        assert_eq!(state.removals.len(), 2);
        assert!(state.removals.contains_key("preparing"));
        assert!(state.removals.contains_key("existing-removal"));
        assert!(!state.removals.contains_key("reused"));
        assert_eq!(state.exits.len(), 4);
        assert_eq!(state.exits[0].exit.run_id, "previous");
        assert!(
            state
                .exits
                .windows(2)
                .all(|pair| pair[0].ended_at_ms <= pair[1].ended_at_ms)
        );
        for recovered in &state.exits[1..] {
            assert_eq!(recovered.exit.reason, ExitReason::PrepareFailed);
            assert!(!recovered.exit.launched);
            assert!(recovered.exit.workspace.is_none());
            assert!(recovered.exit.exit_code.is_none());
        }
        state.clone()
    };
    let reopened = NodeAgents::open(home.path())?;
    assert_eq!(
        *reopened.state.lock().await,
        recovered,
        "recovery must persist and be idempotent"
    );
    Ok(())
}
