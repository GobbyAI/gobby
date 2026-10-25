#![cfg(unix)]

#[path = "node_support/fake_gterm.rs"]
mod fake_gterm;
#[path = "node_support/workspace_fixtures.rs"]
mod workspace_fixtures;

use anyhow::Result;
use gobby_core::bootstrap::parse_hub_database_bootstrap;
use gobby_daemon::node_commands::{
    ExitReason, ReportExit, SpawnMode, SpawnWorkspace, WorkspaceMode,
};
use gobby_daemon::nodes::agents::{
    NodeAgents,
    repair::{self, RepairEnv},
    state::{AgentState, ExitRecord, RunPhase, RunRecord, WorkspaceRecord},
    workspace,
};
use gobby_daemon::state::NodeServices;
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
use workspace_fixtures::{
    FIXTURE_HOME, FIXTURE_HOOKS_DIR, PROJECT_ID, clone_of, fake_uv, git, golden_path,
    is_git_ignored, source_repo,
};

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
        base_branch: Some("main".into()),
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

fn never() -> bool {
    false
}

fn always() -> bool {
    true
}

fn repair_env<'a>(home: &'a Path, hooks_dir: &'a Path, claude_json: &'a Path) -> RepairEnv<'a> {
    RepairEnv {
        home,
        hooks_dir,
        claude_json,
        uv: None,
        seed_timeout: Duration::from_secs(180),
    }
}

/// Gobby home with a canonical spelling, so record paths match what git reports.
fn canonical_home(root: &Path) -> Result<PathBuf> {
    let home = root.join("home");
    fs::create_dir_all(&home)?;
    Ok(fs::canonicalize(home)?)
}

fn worktree_spawn(branch: &str) -> SpawnWorkspace {
    SpawnWorkspace {
        mode: SpawnMode::Worktree,
        path: None,
        branch_name: Some(branch.into()),
        base_branch: None,
    }
}

fn clone_spawn(branch: &str) -> SpawnWorkspace {
    SpawnWorkspace {
        mode: SpawnMode::Clone,
        path: None,
        branch_name: Some(branch.into()),
        base_branch: None,
    }
}

fn existing_spawn(path: &Path, branch: &str) -> SpawnWorkspace {
    SpawnWorkspace {
        mode: SpawnMode::Existing,
        path: Some(path.display().to_string()),
        branch_name: Some(branch.into()),
        base_branch: None,
    }
}

async fn accept_run(agents: &NodeAgents, run_id: &str) -> Result<()> {
    let mut state = agents.state.lock().await;
    state
        .runs
        .insert(run_id.into(), run(RunPhase::Accepted, None));
    state.commit(&agents.home, &agents.changed)?;
    Ok(())
}

fn persisted_state(home: &Path) -> Result<AgentState> {
    Ok(serde_json::from_slice(&fs::read(
        home.join("node/agents.json"),
    )?)?)
}

/// The `[sync]` block of a fake `uv` log, or empty when `sync` never ran.
fn sync_block(log: &Path) -> String {
    fs::read_to_string(log)
        .unwrap_or_default()
        .split("argv: ")
        .find(|block| block.starts_with("[sync]"))
        .unwrap_or_default()
        .to_owned()
}

fn add_worktree(source: &Path, record: &WorkspaceRecord) -> Result<()> {
    let path = record.path.display().to_string();
    fs::create_dir_all(record.path.parent().expect("workspace has a parent"))?;
    git(
        source,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            record.branch_name.as_str(),
            path.as_str(),
            "main",
        ],
    )?;
    Ok(())
}

#[tokio::test]
async fn worktree_prepared_with_record_first() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    let home = canonical_home(root.path())?;
    let claude_json = root.path().join("claude.json");
    let hooks_dir = home.join("hooks");
    let env = repair_env(&home, &hooks_dir, &claude_json);

    let planned = workspace::plan_workspace(
        "run-1",
        PROJECT_ID,
        &source,
        &worktree_spawn("agent/feature"),
        &home,
    )?
    .expect("worktree mode plans a record");
    assert_eq!(
        planned.path,
        home.join("worktrees").join(PROJECT_ID).join("run-1")
    );
    assert_eq!(planned.branch_name, "agent/feature-run-1");
    assert_eq!(planned.base_branch.as_deref(), Some("main"));
    assert!(!planned.reused);

    let agents = NodeAgents::open(&home)?;
    accept_run(&agents, "run-1").await?;

    // A missing base makes `git worktree add` fail; the record must already be on disk.
    let broken = WorkspaceRecord {
        base_branch: Some("no-such-base".into()),
        ..planned.clone()
    };
    workspace::prepare(&agents, "run-1", &broken, "codex", &env, &never)
        .await
        .expect_err("a missing base must fail preparation");
    let persisted = persisted_state(&home)?;
    let on_disk = persisted.runs.get("run-1").expect("run is persisted");
    assert_eq!(on_disk.phase, RunPhase::Preparing);
    assert_eq!(on_disk.workspace.as_ref(), Some(&broken));
    assert!(!broken.path.exists(), "no worktree exists when git fails");

    workspace::prepare(&agents, "run-1", &planned, "codex", &env, &never).await?;
    assert!(planned.path.join("README.md").is_file());
    let worktrees = git(&source, &["worktree", "list", "--porcelain"])?;
    assert!(worktrees.contains(&planned.path.display().to_string()));
    assert!(
        !git(&source, &["branch", "--list", "agent/feature-run-1"])?
            .trim()
            .is_empty()
    );
    Ok(())
}

#[tokio::test]
async fn prepare_cancelled_before_create_keeps_record_without_workspace() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    let home = canonical_home(root.path())?;
    let claude_json = root.path().join("claude.json");
    let hooks_dir = home.join("hooks");
    let env = repair_env(&home, &hooks_dir, &claude_json);

    let planned = workspace::plan_workspace(
        "run-11",
        PROJECT_ID,
        &source,
        &worktree_spawn("agent/cancelled"),
        &home,
    )?
    .expect("worktree mode plans a record");
    let agents = NodeAgents::open(&home)?;
    accept_run(&agents, "run-11").await?;

    let error = workspace::prepare(&agents, "run-11", &planned, "codex", &env, &always)
        .await
        .expect_err("a cancelled preparation must not proceed");
    assert!(format!("{error:#}").contains("cancelled"), "{error:#}");
    let persisted = persisted_state(&home)?;
    let on_disk = persisted.runs.get("run-11").expect("run is persisted");
    assert_eq!(on_disk.phase, RunPhase::Preparing);
    assert_eq!(on_disk.workspace.as_ref(), Some(&planned));
    assert!(
        !planned.path.exists(),
        "no worktree exists when cancelled before create"
    );
    let worktrees = git(&source, &["worktree", "list", "--porcelain"])?;
    assert!(!worktrees.contains(&planned.path.display().to_string()));
    Ok(())
}

#[tokio::test]
async fn prepare_cancelled_after_create_skips_repair() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    let home = canonical_home(root.path())?;
    let claude_json = root.path().join("claude.json");
    let hooks_dir = home.join("hooks");
    let env = repair_env(&home, &hooks_dir, &claude_json);

    let planned = workspace::plan_workspace(
        "run-12",
        PROJECT_ID,
        &source,
        &worktree_spawn("agent/cancel-late"),
        &home,
    )?
    .expect("worktree mode plans a record");
    let agents = NodeAgents::open(&home)?;
    accept_run(&agents, "run-12").await?;

    // The first check runs before create and passes; the second runs between create and repair.
    let checks = AtomicUsize::new(0);
    let cancel = || checks.fetch_add(1, Ordering::SeqCst) >= 1;
    let error = workspace::prepare(&agents, "run-12", &planned, "codex", &env, &cancel)
        .await
        .expect_err("cancellation after create must stop preparation");
    assert!(format!("{error:#}").contains("cancelled"), "{error:#}");
    assert_eq!(checks.load(Ordering::SeqCst), 2);
    assert!(
        planned.path.join("README.md").is_file(),
        "create ran before the cancel"
    );
    assert!(
        !planned.path.join(".mcp.json").exists(),
        "repair must not run after cancel"
    );
    let persisted = persisted_state(&home)?;
    let on_disk = persisted.runs.get("run-12").expect("run is persisted");
    assert_eq!(on_disk.phase, RunPhase::Preparing);
    assert_eq!(on_disk.workspace.as_ref(), Some(&planned));
    Ok(())
}

#[tokio::test]
async fn clone_prepared_with_record_first() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    let home = canonical_home(root.path())?;
    let claude_json = root.path().join("claude.json");
    let hooks_dir = home.join("hooks");
    let env = repair_env(&home, &hooks_dir, &claude_json);

    let planned = workspace::plan_workspace(
        "run-2",
        PROJECT_ID,
        &source,
        &clone_spawn("agent/clone"),
        &home,
    )?
    .expect("clone mode plans a record");
    assert_eq!(
        planned.path,
        home.join("clones").join(PROJECT_ID).join("run-2")
    );
    assert_eq!(planned.branch_name, "agent/clone-run-2");

    let agents = NodeAgents::open(&home)?;
    accept_run(&agents, "run-2").await?;

    let broken = WorkspaceRecord {
        base_branch: Some("no-such-base".into()),
        ..planned.clone()
    };
    workspace::prepare(&agents, "run-2", &broken, "codex", &env, &never)
        .await
        .expect_err("a missing base must fail preparation");
    let persisted = persisted_state(&home)?;
    let on_disk = persisted.runs.get("run-2").expect("run is persisted");
    assert_eq!(on_disk.phase, RunPhase::Preparing);
    assert_eq!(on_disk.workspace.as_ref(), Some(&broken));
    assert!(!broken.path.exists(), "no clone exists when git fails");

    workspace::prepare(&agents, "run-2", &planned, "codex", &env, &never).await?;
    assert!(planned.path.join("README.md").is_file());
    assert_eq!(
        git(&planned.path, &["rev-parse", "--abbrev-ref", "HEAD"])?.trim(),
        planned.branch_name
    );
    Ok(())
}

#[tokio::test]
async fn workspace_repair_per_provider() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    fs::create_dir_all(source.join(".claude"))?;
    fs::write(
        source.join(".claude/settings.json"),
        "{\"from\": \"source\"}\n",
    )?;
    fs::create_dir_all(source.join(".codex"))?;
    fs::write(source.join(".codex/config.toml"), "model = \"fixture\"\n")?;
    let home = canonical_home(root.path())?;
    let claude_json = root.path().join("claude.json");
    let hooks_dir = home.join("hooks");
    let env = repair_env(&home, &hooks_dir, &claude_json);

    for provider in ["claude", "codex", "grok", "droid", "agy"] {
        let workspace = clone_of(
            &source,
            &home.join("worktrees").join(PROJECT_ID).join(provider),
        )?;
        repair::repair(&source, &workspace, provider, &env).await?;

        assert!(
            workspace.join(".gobby/project.json").is_file(),
            "{provider}: project json"
        );
        assert!(
            workspace.join(".gobby/isolation.json").is_file(),
            "{provider}: isolation marker"
        );
        assert!(
            workspace.join(".mcp.json").is_file(),
            "{provider}: MCP config"
        );
        assert!(
            is_git_ignored(&workspace, ".mcp.json"),
            "{provider}: .mcp.json excluded"
        );
        assert!(
            is_git_ignored(&workspace, ".gobby/project.json"),
            "{provider}: project json excluded"
        );
        match provider {
            "claude" => {
                assert_eq!(
                    fs::read(workspace.join(".claude/settings.json"))?,
                    b"{\"from\": \"source\"}\n"
                );
                assert!(
                    is_git_ignored(&workspace, ".claude/settings.json"),
                    "claude: excluded"
                );
                assert!(!workspace.join(".codex").exists());
                assert!(!workspace.join(".factory").exists());
            }
            "codex" => {
                assert_eq!(
                    fs::read(workspace.join(".codex/config.toml"))?,
                    b"model = \"fixture\"\n"
                );
                assert!(
                    is_git_ignored(&workspace, ".codex/config.toml"),
                    "codex: excluded"
                );
                assert!(!workspace.join(".claude").exists());
                assert!(!workspace.join(".factory").exists());
            }
            "droid" => {
                assert!(workspace.join(".factory/hooks/hooks.json").is_file());
                assert!(!workspace.join(".claude").exists());
                assert!(!workspace.join(".codex").exists());
            }
            _ => {
                // grok and agy have no hook directory to copy.
                for dir in [".claude", ".codex", ".factory"] {
                    assert!(!workspace.join(dir).exists(), "{provider}: no {dir}");
                }
            }
        }
    }
    Ok(())
}

#[tokio::test]
async fn failed_removal_keeps_record_and_retries() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    let home = canonical_home(root.path())?;
    let claude_json = root.path().join("claude.json");
    let hooks_dir = home.join("hooks");
    let env = repair_env(&home, &hooks_dir, &claude_json);

    let record = workspace::plan_workspace(
        "run-4",
        PROJECT_ID,
        &source,
        &worktree_spawn("agent/removal"),
        &home,
    )?
    .expect("worktree mode plans a record");
    let agents = NodeAgents::open(&home)?;
    accept_run(&agents, "run-4").await?;
    workspace::prepare(&agents, "run-4", &record, "codex", &env, &never).await?;
    {
        let mut state = agents.state.lock().await;
        state.removals.insert("run-4".into(), record.clone());
        state.commit(&agents.home, &agents.changed)?;
    }

    // A read-only parent makes the worktree removal fail.
    let parent = record
        .path
        .parent()
        .expect("workspace has a parent")
        .to_path_buf();
    fs::set_permissions(&parent, fs::Permissions::from_mode(0o555))?;
    workspace::retry_removals(&agents).await?;
    fs::set_permissions(&parent, fs::Permissions::from_mode(0o755))?;
    assert!(
        agents.state.lock().await.removals.contains_key("run-4"),
        "a failed removal keeps its record"
    );
    assert!(record.path.exists(), "a failed removal leaves the worktree");

    workspace::retry_removals(&agents).await?;
    assert!(
        !agents.state.lock().await.removals.contains_key("run-4"),
        "a successful retry deletes the record"
    );
    assert!(!record.path.exists());
    assert!(
        git(&source, &["branch", "--list", "agent/removal-run-4"])?
            .trim()
            .is_empty()
    );

    let gone = WorkspaceRecord {
        path: root.path().join("never-created"),
        branch_name: "agent/never-created".into(),
        ..record.clone()
    };
    workspace::remove(&gone).await?;
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Boundary {
    AfterRecord,
    AfterWorktreeAdd,
    DuringRemoval,
}

#[tokio::test]
async fn restart_at_each_boundary_cleans_up() -> Result<()> {
    for boundary in [
        Boundary::AfterRecord,
        Boundary::AfterWorktreeAdd,
        Boundary::DuringRemoval,
    ] {
        let root = tempfile::tempdir()?;
        let source = source_repo(root.path())?;
        let home = canonical_home(root.path())?;
        let record = workspace::plan_workspace(
            "run-5",
            PROJECT_ID,
            &source,
            &worktree_spawn("agent/restart"),
            &home,
        )?
        .expect("worktree mode plans a record");
        if boundary != Boundary::AfterRecord {
            add_worktree(&source, &record)?;
        }
        {
            let agents = NodeAgents::open(&home)?;
            let mut state = agents.state.lock().await;
            match boundary {
                Boundary::AfterRecord | Boundary::AfterWorktreeAdd => {
                    state.runs.insert(
                        "run-5".into(),
                        run(RunPhase::Preparing, Some(record.clone())),
                    );
                }
                Boundary::DuringRemoval => {
                    state.removals.insert("run-5".into(), record.clone());
                }
            }
            state.commit(&agents.home, &agents.changed)?;
        }

        // Restart: recovery moves preparing runs to removals, then startup retries them.
        let agents = NodeAgents::open(&home)?;
        workspace::retry_removals(&agents).await?;
        assert!(
            agents.state.lock().await.removals.is_empty(),
            "{boundary:?}: the record is deleted"
        );
        assert!(
            persisted_state(&home)?.removals.is_empty(),
            "{boundary:?}: the deletion is persisted"
        );
        assert!(
            !record.path.exists(),
            "{boundary:?}: the worktree is removed"
        );
        assert!(
            git(&source, &["branch", "--list", record.branch_name.as_str()])?
                .trim()
                .is_empty(),
            "{boundary:?}: the branch is removed"
        );
    }
    Ok(())
}

#[tokio::test]
async fn droid_hooks_match_python_golden() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    let workspace = clone_of(&source, &root.path().join("workspace"))?;
    let hooks = workspace.join(".factory/hooks/hooks.json");
    fs::create_dir_all(hooks.parent().expect("hooks file has a parent"))?;
    fs::copy(golden_path("droid_hooks_existing.json"), &hooks)?;
    let claude_json = root.path().join("claude.json");
    let hooks_dir = PathBuf::from(FIXTURE_HOOKS_DIR);
    let env = RepairEnv {
        home: Path::new(FIXTURE_HOME),
        hooks_dir: &hooks_dir,
        claude_json: &claude_json,
        uv: None,
        seed_timeout: Duration::from_secs(1),
    };

    repair::repair(&source, &workspace, "droid", &env).await?;

    assert_eq!(
        fs::read(&hooks)?,
        fs::read(golden_path("droid_hooks.json"))?,
        "the droid hooks file matches the Python golden byte for byte"
    );
    Ok(())
}

fn python_workspace(
    source: &Path,
    home: &Path,
    name: &str,
    pyproject: bool,
    lock: bool,
) -> Result<PathBuf> {
    let workspace = clone_of(source, &home.join("worktrees").join(PROJECT_ID).join(name))?;
    if pyproject {
        fs::write(
            workspace.join("pyproject.toml"),
            "[project]\nname = \"fixture\"\nversion = \"0.0.0\"\n",
        )?;
    }
    if lock {
        fs::write(workspace.join("uv.lock"), "version = 1\n")?;
    }
    Ok(workspace)
}

#[tokio::test]
async fn python_seed_matches_python_steps() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    let home = canonical_home(root.path())?;
    let claude_json = root.path().join("claude.json");
    let hooks_dir = home.join("hooks");
    let cache = root.path().join("uv-cache");
    fs::create_dir_all(&cache)?;

    let workspace = python_workspace(&source, &home, "full", true, true)?;
    let (uv, log) = fake_uv(&root.path().join("uv-full"), &cache, "exit 0")?;
    let env = RepairEnv {
        uv: Some(uv.as_path()),
        ..repair_env(&home, &hooks_dir, &claude_json)
    };
    repair::repair(&source, &workspace, "codex", &env).await?;
    let sync = sync_block(&log);
    let argv = format!(
        "[sync] [--offline] [--frozen] [--no-progress] [--link-mode] [copy] [--cache-dir] [{}]",
        cache.display()
    );
    assert!(sync.starts_with(&argv), "full seed argv: {sync}");
    assert!(sync.contains("UV_PYTHON_DOWNLOADS=never"), "{sync}");
    for cleared in [
        "UV_CACHE_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
    ] {
        assert!(
            sync.contains(&format!("{cleared}=unset")),
            "{cleared} is cleared: {sync}"
        );
    }

    // Each of these skips the seed: no pyproject.toml, no uv.lock, no uv, no cache directory.
    for (name, pyproject, lock, with_uv, cache_exists) in [
        ("no-pyproject", false, true, true, true),
        ("no-lock", true, false, true, true),
        ("no-uv", true, true, false, true),
        ("no-cache", true, true, true, false),
    ] {
        let workspace = python_workspace(&source, &home, name, pyproject, lock)?;
        let cache_dir = if cache_exists {
            cache.clone()
        } else {
            root.path().join("never-created")
        };
        let (uv_bin, uv_log) = fake_uv(
            &root.path().join(format!("uv-{name}")),
            &cache_dir,
            "exit 0",
        )?;
        let env = RepairEnv {
            uv: with_uv.then_some(uv_bin.as_path()),
            ..repair_env(&home, &hooks_dir, &claude_json)
        };
        repair::repair(&source, &workspace, "codex", &env).await?;
        assert!(
            sync_block(&uv_log).is_empty(),
            "{name}: the seed is skipped"
        );
        assert!(
            workspace.join(".mcp.json").is_file(),
            "{name}: repair continues"
        );
    }

    // A failing sync only warns; repair keeps going.
    let workspace = python_workspace(&source, &home, "failed-sync", true, true)?;
    let (uv_bin, uv_log) = fake_uv(&root.path().join("uv-failed"), &cache, "exit 1")?;
    let env = RepairEnv {
        uv: Some(uv_bin.as_path()),
        ..repair_env(&home, &hooks_dir, &claude_json)
    };
    repair::repair(&source, &workspace, "codex", &env).await?;
    assert!(!sync_block(&uv_log).is_empty(), "failed-sync: sync ran");
    assert!(
        workspace.join(".mcp.json").is_file(),
        "a failed sync leaves repair running"
    );

    // A sync past its timeout is abandoned, not awaited.
    let workspace = python_workspace(&source, &home, "timeout", true, true)?;
    let (uv_bin, uv_log) = fake_uv(&root.path().join("uv-timeout"), &cache, "sleep 5")?;
    let env = RepairEnv {
        uv: Some(uv_bin.as_path()),
        seed_timeout: Duration::from_millis(200),
        ..repair_env(&home, &hooks_dir, &claude_json)
    };
    let started = Instant::now();
    repair::repair(&source, &workspace, "codex", &env).await?;
    assert!(
        started.elapsed() < Duration::from_secs(4),
        "a timed-out sync is abandoned, not awaited"
    );
    assert!(!sync_block(&uv_log).is_empty(), "timeout: sync ran");
    assert!(
        workspace.join(".mcp.json").is_file(),
        "a timed-out sync leaves repair running"
    );
    Ok(())
}

#[tokio::test]
async fn mcp_config_names_gmcp() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    let home = canonical_home(root.path())?;
    let hooks_dir = home.join("hooks");
    let claude_json = root.path().join("claude.json");
    let workspace = clone_of(
        &source,
        &home.join("worktrees").join(PROJECT_ID).join("mcp"),
    )?;
    fs::write(
        workspace.join(".mcp.json"),
        r#"{"mcpServers": {"other": {"command": "other-bin", "args": []}}}"#,
    )?;
    fs::write(
        &claude_json,
        r#"{"projects": {"/elsewhere": {"mcpServers": {"keep": {"command": "keep-bin", "args": []}}}}}"#,
    )?;
    let env = repair_env(&home, &hooks_dir, &claude_json);

    repair::repair(&source, &workspace, "claude", &env).await?;

    let gmcp = json!({"command": home.join("bin/gmcp").display().to_string(), "args": []});
    let mcp: Value = serde_json::from_slice(&fs::read(workspace.join(".mcp.json"))?)?;
    assert_eq!(mcp["mcpServers"]["gobby"], gmcp);
    assert_eq!(
        mcp["mcpServers"]["other"]["command"], "other-bin",
        "other servers are kept"
    );

    let claude: Value = serde_json::from_slice(&fs::read(&claude_json)?)?;
    assert_eq!(
        claude["projects"]["/elsewhere"]["mcpServers"]["keep"]["command"],
        "keep-bin"
    );
    assert_eq!(
        claude["projects"][workspace.display().to_string()]["mcpServers"]["gobby"],
        gmcp
    );
    assert!(
        !root.path().join("claude.json.lock").exists(),
        "the mkdir lock is released"
    );
    Ok(())
}

#[tokio::test]
async fn existing_workspace_used_in_place() -> Result<()> {
    let root = tempfile::tempdir()?;
    let source = source_repo(root.path())?;
    let home = canonical_home(root.path())?;
    let claude_json = root.path().join("claude.json");
    let hooks_dir = home.join("hooks");
    let env = repair_env(&home, &hooks_dir, &claude_json);

    // A worktree from an earlier run, with uncommitted work in it.
    let earlier = home.join("worktrees").join(PROJECT_ID).join("earlier-run");
    fs::create_dir_all(earlier.parent().expect("workspace has a parent"))?;
    let earlier_path = earlier.display().to_string();
    git(
        &source,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "agent/resume",
            earlier_path.as_str(),
            "main",
        ],
    )?;
    fs::write(earlier.join("scratch.txt"), "uncommitted\n")?;
    let worktrees_before = git(&source, &["worktree", "list", "--porcelain"])?;
    let branches_before = git(&source, &["branch", "--list"])?;

    let record = workspace::plan_workspace(
        "run-10",
        PROJECT_ID,
        &source,
        &existing_spawn(&earlier, "agent/resume"),
        &home,
    )?
    .expect("an existing workspace plans a record");
    assert_eq!(record.path, earlier);
    assert_eq!(record.mode, WorkspaceMode::Worktree);
    assert_eq!(record.branch_name, "agent/resume");
    assert_eq!(record.base_branch, None);
    assert!(record.reused);

    let agents = NodeAgents::open(&home)?;
    accept_run(&agents, "run-10").await?;
    workspace::prepare(&agents, "run-10", &record, "codex", &env, &never).await?;

    assert_eq!(
        fs::read_to_string(earlier.join("scratch.txt"))?,
        "uncommitted\n"
    );
    assert_eq!(
        git(&source, &["worktree", "list", "--porcelain"])?,
        worktrees_before
    );
    assert_eq!(git(&source, &["branch", "--list"])?, branches_before);
    assert!(
        earlier.join(".gobby/project.json").is_file(),
        "repair runs on the reused path"
    );

    // A missing path, or one outside the worktrees and clones roots, fails naming the path.
    let missing = home.join("worktrees").join(PROJECT_ID).join("gone");
    let err = workspace::plan_workspace(
        "run-11",
        PROJECT_ID,
        &source,
        &existing_spawn(&missing, "agent/gone"),
        &home,
    )
    .expect_err("a missing existing path fails preparation");
    assert!(
        format!("{err:#}").contains(&missing.display().to_string()),
        "{err:#}"
    );

    let outside_dir = root.path().join("elsewhere");
    fs::create_dir_all(&outside_dir)?;
    let outside = fs::canonicalize(&outside_dir)?;
    let err = workspace::plan_workspace(
        "run-12",
        PROJECT_ID,
        &source,
        &existing_spawn(&outside, "agent/outside"),
        &home,
    )
    .expect_err("a path outside the roots fails preparation");
    assert!(
        format!("{err:#}").contains(&outside.display().to_string()),
        "{err:#}"
    );
    Ok(())
}
