//! Frozen launch goldens and isolated node/host acceptance tests for #24375.
use super::*;
use gobby_daemon::node_commands::SpawnPayload;
use gobby_daemon::nodes::agents::{
    agy_gate::ensure_agy_support,
    launch::{LaunchContext, launch_with, reconcile, watch_exits},
    provider_command::build_command,
    provider_env::{EnvInputs, build_env},
    sandbox::runtime::SandboxLaunch,
    trust::seed_trust,
};
use std::sync::Arc;

fn corpus() -> Result<Value> {
    Ok(serde_json::from_str(include_str!(
        "../fixtures/node_launch/corpus.json"
    ))?)
}

struct CommandCase {
    _root: tempfile::TempDir,
    cwd: PathBuf,
    home: PathBuf,
    payload: SpawnPayload,
    sandbox: SandboxLaunch,
    fixture: Value,
    expected: Value,
}

impl CommandCase {
    fn new(name: &str) -> Result<Self> {
        let corpus = corpus()?;
        let root = tempfile::tempdir()?;
        let cwd = root.path().join("workspace");
        let home = root.path().join("gobby-home");
        fs::create_dir_all(&cwd)?;
        fs::create_dir_all(&home)?;
        // Substitute paths in JSON strings, preserving all frozen values and ordering.
        let filled = serde_json::to_string(&corpus)?
            .replace("<workspace>", &cwd.display().to_string())
            .replace("<gobby_home>", &home.display().to_string());
        let filled: Value = serde_json::from_str(&filled)?;
        let case = filled["cases"]
            .as_array()
            .context("corpus cases")?
            .iter()
            .find(|case| case["name"] == name)
            .context("named corpus case")?;
        if case["inputs"]["mcp_config"] == true {
            fs::write(cwd.join(".mcp.json"), "{}")?;
        }
        let payload: SpawnPayload = serde_json::from_value(case["inputs"]["payload"].clone())?;
        let sandbox = SandboxLaunch {
            node: home.join("bin/node"),
            runner: home.join("tools/srt/runner.mjs"),
            settings: home.join("run/settings.json"),
            violations: home.join("run/violations.jsonl"),
            policy_hash: "fixture-policy".into(),
            provider_executable: home.join("bin").join(&payload.provider),
            provider_args: serde_json::from_value(case["inputs"]["sandbox_args"].clone())?,
            env: serde_json::from_value(filled["fixture"]["sandbox_env"].clone())?,
            read_paths: vec![],
            write_paths: vec![],
            deny_paths: vec![],
        };
        Ok(Self {
            _root: root,
            cwd,
            home,
            payload,
            sandbox,
            fixture: filled["fixture"].clone(),
            expected: case["expected"].clone(),
        })
    }

    fn environment(&self) -> Result<gobby_daemon::nodes::agents::provider_env::ProviderEnv> {
        build_env(
            &self.payload,
            &EnvInputs {
                cwd: &self.cwd,
                gobby_home: &self.home,
                front_door_url: self.fixture["front_door_url"]
                    .as_str()
                    .context("front door")?,
                prompt_file: Path::new(
                    self.fixture["prompt_file"]
                        .as_str()
                        .context("prompt file")?,
                ),
                cache_env: &serde_json::from_value(self.fixture["cache_env"].clone())?,
                sandbox_launch: &self.sandbox,
                ambient: &serde_json::from_value(self.fixture["ambient"].clone())?,
            },
        )
    }
}

// Each case runs independently so a failure in one provider cannot hide another.
macro_rules! command_case {
    ($test:ident, $name:literal) => {
        #[test]
        fn $test() -> Result<()> {
            let case = CommandCase::new($name)?;
            let environment = case.environment()?;
            assert_eq!(
                build_command(
                    &case.payload,
                    &case.cwd,
                    &case.home,
                    &case.sandbox,
                    &environment.env
                )?,
                serde_json::from_value::<Vec<String>>(case.expected["command"].clone())?,
                "{} command differs from the frozen oracle",
                $name
            );
            Ok(())
        }
    };
}
command_case!(provider_commands_match_python_claude_fresh, "claude-fresh");
command_case!(
    provider_commands_match_python_claude_resume,
    "claude-resume"
);
command_case!(provider_commands_match_python_codex_fresh, "codex-fresh");
command_case!(provider_commands_match_python_codex_resume, "codex-resume");
command_case!(provider_commands_match_python_codex_oss, "codex-oss");
command_case!(provider_commands_match_python_grok_agent, "grok-agent");
command_case!(
    provider_commands_match_python_grok_terminal,
    "grok-terminal"
);
command_case!(provider_commands_match_python_grok_resume, "grok-resume");
command_case!(provider_commands_match_python_droid_exec, "droid-exec");
command_case!(
    provider_commands_match_python_droid_resume,
    "droid-exec-resume"
);
command_case!(
    provider_commands_match_python_droid_interactive,
    "droid-interactive"
);
command_case!(
    provider_commands_match_python_droid_interactive_resume,
    "droid-interactive-resume"
);
command_case!(provider_commands_match_python_agy_fresh, "agy-fresh");
command_case!(provider_commands_match_python_agy_resume, "agy-resume");

#[test]
fn provider_env_layers() -> Result<()> {
    for entry in corpus()?["cases"].as_array().context("corpus cases")? {
        let name = entry["name"].as_str().context("case name")?;
        let case = CommandCase::new(name)?;
        let actual = case.environment()?;
        assert_eq!(
            actual.env,
            serde_json::from_value::<BTreeMap<String, String>>(case.expected["env"].clone())?,
            "{name}: environment"
        );
        assert_eq!(
            actual.argv_prefix,
            serde_json::from_value::<Vec<String>>(case.expected["argv_prefix"].clone())?,
            "{name}: denied ambient unsets"
        );
        assert!(!actual.env.contains_key("GOBBY_AGENT_API_TOKEN"), "{name}");
        assert_ne!(
            actual.env.get("GOBBY_SESSION_ID").map(String::as_str),
            Some("reserved-session")
        );
    }
    Ok(())
}

#[test]
fn codex_prompt_placement() -> Result<()> {
    for name in ["codex-fresh", "codex-resume", "codex-oss"] {
        let mut case = CommandCase::new(name)?;
        // This retired role must obey the same UI prompt contract as every node agent.
        case.payload.agent_name = Some("task-close-reviewer".into());
        let environment = case.environment()?;
        let argv = build_command(
            &case.payload,
            &case.cwd,
            &case.home,
            &case.sandbox,
            &environment.env,
        )?;
        assert!(
            !argv.contains(&case.payload.prompt),
            "{name}: prompt leaked to argv"
        );
        assert!(
            !argv
                .iter()
                .any(|arg| arg == "exec" || arg == "--dangerously-bypass-approvals-and-sandbox")
        );
        assert!(
            argv.windows(2)
                .any(|args| args == ["--ask-for-approval", "never"])
        );
        assert!(
            argv.windows(2)
                .any(|args| args == ["--disable", "guardian_approval"])
        );
    }
    Ok(())
}

fn executable(path: &Path, body: &str) -> Result<()> {
    fs::write(path, format!("#!/bin/sh\n{body}\n"))?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o755))?;
    Ok(())
}

#[test]
fn agy_version_gate() -> Result<()> {
    let root = tempfile::tempdir()?;
    let path = root.path().display().to_string();
    let binary = root.path().join("agy");
    let absent = ensure_agy_support(&path, root.path()).expect_err("absent agy must refuse");
    assert!(absent.contains("1.1.18"), "{absent}");
    for version in ["1.1.17", "unparseable"] {
        executable(&binary, &format!("echo '{version}'"))?;
        let reason = ensure_agy_support(&path, root.path()).expect_err("unsupported agy");
        assert!(reason.contains("1.1.18"), "{reason}");
    }
    let probes = root.path().join("probes");
    executable(
        &binary,
        &format!(
            "echo probe >> '{}'; echo 'Antigravity v1.1.18'",
            probes.display()
        ),
    )?;
    ensure_agy_support(&path, root.path()).map_err(anyhow::Error::msg)?;
    ensure_agy_support(&path, root.path()).map_err(anyhow::Error::msg)?;
    assert_eq!(
        fs::read_to_string(&probes)?.lines().count(),
        1,
        "unchanged binary should use cache"
    );
    let replacement = root.path().join("replacement");
    executable(&replacement, "echo 'Antigravity v1.0.0'")?;
    fs::rename(replacement, binary)?;
    let reason = ensure_agy_support(&path, root.path()).expect_err("replacement must be reprobed");
    assert!(reason.contains("1.0.0"), "{reason}");
    Ok(())
}

#[test]
fn trust_seeds_workspace_and_realpath() -> Result<()> {
    let root = tempfile::tempdir()?;
    let home = root.path().join("home");
    let real = root.path().join("real-workspace");
    let alias = root.path().join("workspace-link");
    fs::create_dir_all(home.join(".codex"))?;
    fs::create_dir_all(&real)?;
    std::os::unix::fs::symlink(&real, &alias)?;
    fs::write(
        home.join(".claude.json"),
        r#"{"unrelated":42,"projects":{"/keep":{"hasTrustDialogAccepted":true}}}"#,
    )?;
    fs::write(
        home.join(".codex/config.toml"),
        "model = \"keep-model\"\n[projects.\"/keep\"]\ntrust_level = \"trusted\"\n",
    )?;
    for provider in ["claude", "codex"] {
        seed_trust(provider, &home, &alias)?;
    }
    let claude: Value = serde_json::from_slice(&fs::read(home.join(".claude.json"))?)?;
    assert_eq!(claude["unrelated"], 42);
    assert_eq!(claude["projects"]["/keep"]["hasTrustDialogAccepted"], true);
    for path in [&alias, &fs::canonicalize(&real)?] {
        assert_eq!(
            claude["projects"][path.display().to_string()]["hasTrustDialogAccepted"],
            true
        );
    }
    let codex: toml::Value = toml::from_str(&fs::read_to_string(home.join(".codex/config.toml"))?)?;
    assert_eq!(codex["model"].as_str(), Some("keep-model"));
    for path in [
        Path::new("/keep"),
        alias.as_path(),
        fs::canonicalize(&real)?.as_path(),
    ] {
        assert_eq!(
            codex["projects"][path.to_str().context("trust path")?]["trust_level"].as_str(),
            Some("trusted")
        );
    }
    Ok(())
}

struct LaunchFixture {
    tree: SrtTree,
    context: LaunchContext,
    payload: SpawnPayload,
    agents: Arc<NodeAgents>,
    host: fake_gterm::FakeGterm,
    _monitor: StopMonitor,
    // Keep the pidfile alive until StopMonitor has stopped the isolated monitor.
    _root: tempfile::TempDir,
}

impl LaunchFixture {
    async fn new() -> Result<Self> {
        let root = tempfile::tempdir()?;
        let (mut tree, inputs) = chokepoint_fixture(&root.path().join("case"), "claude", |_| {})?;
        // The nextest TMPDIR is already long on macOS; keep the control socket <104 bytes.
        let gobby_home = fs::canonicalize(root.path())?.join("g");
        fs::create_dir_all(&gobby_home)?;
        tree.layout
            .insert("gobby_home", gobby_home.display().to_string());
        tree.layout.insert(
            "run_root",
            gobby_home
                .join("run/sandbox")
                .join(SRT_RUN_ID)
                .display()
                .to_string(),
        );
        let snapshot = tree.path("gobby_home").join("prompt-modes");
        // Observe the original prompt while prepare_launch runs its preflight.
        let stat_args = if cfg!(target_os = "macos") {
            "-f '%Lp'"
        } else {
            "-c '%a'"
        };
        let body = format!(
            "/usr/bin/stat {stat_args} '{}' '{}' > '{}'\nexit 0\n",
            tree.path("gobby_home").join("node/prompts").display(),
            tree.path("gobby_home")
                .join(format!("node/prompts/{SRT_RUN_ID}.md"))
                .display(),
            snapshot.display()
        );
        let node = monitor_node(&tree, &body)?;
        let installation = srt_installation(&tree, node);
        let mut payload = CommandCase::new("claude-fresh")?.payload;
        payload.run_id = SRT_RUN_ID.into();
        payload.source_root = inputs.source_root.display().to_string();
        payload.project_id = inputs.project_id.clone();
        payload.session_id = inputs.session_id.clone();
        payload.sandbox = inputs.sandbox;
        payload.extra_env.clear();
        let context = LaunchContext {
            installation,
            front_door_url: "http://127.0.0.1:60999".into(),
            home: tree.path("home"),
            path_env: format!("{}:/usr/bin:/bin", tree.path("bin").display()),
            ambient: inputs.env,
            cache_env: BTreeMap::new(),
            daemon_port: inputs.daemon_port,
            websocket_port: inputs.websocket_port,
            platform: srt_host_platform().into(),
        };
        let home = tree.path("gobby_home");
        let host = fake_gterm::FakeGterm::start(&home).await?;
        let agents = Arc::new(NodeAgents::open(&home)?);
        {
            let mut state = agents.state.lock().await;
            let mut record = run(RunPhase::Preparing, Some(workspace(root.path(), false)));
            record.terminal_id = payload.terminal_id.clone();
            record.host_terminal_id = None;
            record.workspace.as_mut().context("workspace")?.path = tree.path("workspace");
            state.runs.insert(payload.run_id.clone(), record);
            state.commit(&home, &agents.changed)?;
        }
        let monitor = StopMonitor(monitor_pidfile(&tree));
        Ok(Self {
            _root: root,
            tree,
            context,
            payload,
            agents,
            host,
            _monitor: monitor,
        })
    }

    async fn launch(&self) -> Result<()> {
        launch_with(
            &self.agents,
            &self.context,
            &self.payload.run_id,
            &self.payload,
            &self.tree.path("workspace"),
        )
        .await
        .map_err(|failure| anyhow::anyhow!("{failure:?}"))
    }
}

#[tokio::test]
async fn launch_spawns_and_commits() -> Result<()> {
    let fixture = LaunchFixture::new().await?;
    fixture.launch().await?;
    let requests = fixture.host.requests();
    let spawn = requests
        .iter()
        .position(|r| r["method"] == "spawn")
        .context("spawn")?;
    let commit = requests
        .iter()
        .position(|r| r["method"] == "spawn_commit")
        .context("commit")?;
    assert!(spawn < commit);
    assert_eq!(requests[spawn]["spawn_key"], fixture.payload.run_id);
    assert_eq!(requests[spawn]["commit_deadline_ms"], 30_000);
    let argv = requests[spawn]["argv"].as_array().context("spawn argv")?;
    assert_eq!(
        argv[0],
        fixture.context.installation.node.display().to_string()
    );
    let separator = argv
        .iter()
        .position(|arg| arg == "--")
        .context("SRT separator")?;
    let (wrapper, provider) = argv.split_at(separator);
    assert_eq!(
        wrapper.iter().filter(|arg| **arg == "--settings").count(),
        1
    );
    let corpus = corpus()?;
    let command = corpus["cases"]
        .as_array()
        .context("corpus cases")?
        .iter()
        .find(|case| case["name"] == "claude-fresh")
        .context("claude-fresh corpus case")?["expected"]["command"]
        .as_array()
        .context("Claude command")?;
    let settings = |args: &[Value]| {
        args.windows(2)
            .filter(|pair| pair[0] == "--settings")
            .map(|pair| pair[1].clone())
            .collect::<Vec<_>>()
    };
    assert_eq!(settings(&provider[1..]), settings(command));
    let persisted = persisted_state(&fixture.agents.home)?;
    let run = &persisted.runs[&fixture.payload.run_id];
    assert_eq!(run.phase, RunPhase::Launched);
    assert_eq!(
        run.host_terminal_id.as_deref(),
        Some(format!("host-{}", fixture.payload.terminal_id).as_str())
    );
    // Secrets may enter host memory, never the durable state or SRT assets.
    let token = fixture
        .payload
        .api_token
        .as_ref()
        .context("fixture token")?;
    for path in [
        fixture.agents.home.join("node/agents.json"),
        fixture.tree.path("run_root").join("assets/settings.json"),
    ] {
        assert!(!fs::read_to_string(path)?.contains(token));
    }
    Ok(())
}

#[tokio::test]
async fn launch_removes_prompt_file() -> Result<()> {
    let fixture = LaunchFixture::new().await?;
    fixture.launch().await?;
    let prompts = fixture.agents.home.join("node/prompts");
    assert!(
        !prompts
            .join(format!("{}.md", fixture.payload.run_id))
            .exists()
    );
    assert_eq!(file_mode(&prompts)?, 0o700);
    assert_eq!(
        fs::read_to_string(fixture.agents.home.join("prompt-modes"))?,
        "700\n600\n"
    );
    let copied = fixture.tree.path("run_root").join("assets/prompt.md");
    assert_eq!(file_mode(&copied)?, 0o600);
    assert_eq!(fs::read_to_string(&copied)?, fixture.payload.prompt);
    let requests = fixture.host.requests();
    let spawn = requests
        .iter()
        .find(|r| r["method"] == "spawn")
        .context("spawn")?;
    assert_eq!(
        spawn["env"]["GOBBY_PROMPT_FILE"],
        copied.display().to_string()
    );

    let mut refused = LaunchFixture::new().await?;
    refused.payload.sandbox.enabled = false;
    assert!(refused.launch().await.is_err());
    assert!(
        !refused
            .agents
            .home
            .join(format!("node/prompts/{}.md", refused.payload.run_id))
            .exists()
    );
    assert!(
        !refused
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "spawn")
    );
    Ok(())
}

#[tokio::test]
async fn launch_aborts_prepared_child_on_commit_failure() -> Result<()> {
    let fixture = LaunchFixture::new().await?;
    fixture.host.reject_next_commit();
    let failure = launch_with(
        &fixture.agents,
        &fixture.context,
        &fixture.payload.run_id,
        &fixture.payload,
        &fixture.tree.path("workspace"),
    )
    .await
    .expect_err("commit refused");
    assert_eq!(failure.reason, ExitReason::LaunchFailed);
    let requests = fixture.host.requests();
    let commit = requests
        .iter()
        .position(|r| r["method"] == "spawn_commit")
        .context("commit attempt")?;
    let abort = requests
        .iter()
        .position(|r| r["method"] == "kill")
        .context("prepared child must be aborted")?;
    assert!(commit < abort);
    assert_eq!(
        requests[abort]["host_terminal_id"],
        format!("host-{}", fixture.payload.terminal_id)
    );
    assert_eq!(requests[abort]["grace_ms"], 0);
    assert_ne!(
        persisted_state(&fixture.agents.home)?.runs[&fixture.payload.run_id].phase,
        RunPhase::Launched
    );
    Ok(())
}

#[tokio::test]
async fn launch_trust_failure_only_warns() -> Result<()> {
    let fixture = LaunchFixture::new().await?;
    // A directory at the store path forces a real I/O failure without chmod/root assumptions.
    fs::create_dir_all(fixture.context.home.join(".claude.json"))?;
    fixture.launch().await?;
    assert!(
        fixture
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "spawn_commit")
    );
    Ok(())
}

#[tokio::test]
async fn launch_seeds_provider_trust() -> Result<()> {
    for provider in ["claude", "codex"] {
        let mut fixture = LaunchFixture::new().await?;
        fixture.payload.provider = provider.into();
        fixture.payload.provider_args.clear();
        fixture.launch().await?;
        let cwd = fixture.tree.path("workspace");
        let key = cwd.to_str().context("workspace path")?;
        if provider == "claude" {
            let store: Value =
                serde_json::from_slice(&fs::read(fixture.context.home.join(".claude.json"))?)?;
            assert_eq!(store["projects"][key]["hasTrustDialogAccepted"], true);
        } else {
            let store: toml::Value = toml::from_str(&fs::read_to_string(
                fixture.context.home.join(".codex/config.toml"),
            )?)?;
            assert_eq!(
                store["projects"][key]["trust_level"].as_str(),
                Some("trusted")
            );
        }
    }
    Ok(())
}

#[test]
fn agy_gate_reprobes_binary_changed_during_probe() -> Result<()> {
    let root = tempfile::tempdir()?;
    let binary = root.path().join("agy");
    let replacement = root.path().join("new-agy");
    executable(&replacement, "echo 'Antigravity v1.1.18'")?;
    executable(
        &binary,
        &format!("/bin/mv '{}' \"$0\"; echo 'v1.1.17'", replacement.display()),
    )?;
    ensure_agy_support(&root.path().display().to_string(), root.path())
        .map_err(anyhow::Error::msg)?;
    Ok(())
}

#[tokio::test]
async fn agy_refusal_is_prepare_failed() -> Result<()> {
    let mut fixture = LaunchFixture::new().await?;
    fixture.payload.provider = "agy".into();
    executable(&fixture.tree.path("bin").join("agy"), "echo 'agy v1.1.17'")?;
    let failure = launch_with(
        &fixture.agents,
        &fixture.context,
        &fixture.payload.run_id,
        &fixture.payload,
        &fixture.tree.path("workspace"),
    )
    .await
    .expect_err("old agy");
    assert_eq!(failure.reason, ExitReason::PrepareFailed);
    assert!(failure.detail.contains("1.1.18"));
    assert!(
        !fixture
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "spawn")
    );
    Ok(())
}

#[tokio::test]
async fn reconcile_after_restart() -> Result<()> {
    for epoch in [fake_gterm::EPOCH, "previous-host-epoch"] {
        let root = tempfile::tempdir()?;
        let home = root.path();
        let agents = NodeAgents::open(home)?;
        {
            let mut state = agents.state.lock().await;
            state.host_epoch_id = Some(epoch.into());
            for id in ["present", "missing"] {
                let mut record = run(RunPhase::Launched, Some(workspace(home, false)));
                record.terminal_id = id.into();
                record.host_terminal_id = Some(format!("host-{id}"));
                state.runs.insert(id.into(), record);
            }
            state.commit(home, &agents.changed)?;
        }
        drop(agents);
        let agents = NodeAgents::open(home)?;
        let _host = fake_gterm::FakeGterm::start_with_inventory(
            home,
            vec![json!({
                "terminal_id":"present", "host_terminal_id":"host-present", "spawn_key":"present"
            })],
        )
        .await?;
        reconcile(&agents).await?;
        let state = persisted_state(home)?;
        assert!(
            state.runs.contains_key("present"),
            "epoch {epoch}: live pane was lost"
        );
        assert!(!state.runs.contains_key("missing"));
        let exit = &state
            .exits
            .iter()
            .find(|exit| exit.exit.run_id == "missing")
            .context("missing exit")?
            .exit;
        assert_eq!(exit.reason, ExitReason::Exited);
        assert_eq!(exit.exit_code, None);
        assert!(exit.launched);
        assert!(exit.workspace.is_some());
    }
    Ok(())
}

#[tokio::test]
async fn exit_events_record_exits() -> Result<()> {
    for cancelled in [false, true] {
        let root = tempfile::tempdir()?;
        let host = fake_gterm::FakeGterm::start_with_inventory(
            root.path(),
            vec![json!({
                "terminal_id":"fixture-terminal", "host_terminal_id":"fixture-host-terminal",
                "spawn_key":"run-exit"
            })],
        )
        .await?;
        let agents = Arc::new(NodeAgents::open(root.path())?);
        {
            let mut state = agents.state.lock().await;
            let mut record = run(RunPhase::Launched, Some(workspace(root.path(), false)));
            record.terminal_id = "fixture-terminal".into();
            record.host_terminal_id = Some("fixture-host-terminal".into());
            state.runs.insert("run-exit".into(), record);
            if cancelled {
                state.cancelled.insert("run-exit".into(), u64::MAX);
            }
            state.commit(root.path(), &agents.changed)?;
        }
        let mut watcher = tokio::spawn({
            let agents = Arc::clone(&agents);
            async move { watch_exits(&agents).await }
        });
        let ready = async {
            while !host
                .requests()
                .iter()
                .any(|r| r["method"] == "subscribe_events")
            {
                tokio::task::yield_now().await;
            }
        };
        tokio::select! {
            result = &mut watcher => { result??; anyhow::bail!("exit watcher stopped before subscribing"); }
            result = tokio::time::timeout(Duration::from_secs(5), ready) => { result?; }
        }
        // A subscriber exists before emission; durable state is the observable result.
        host.terminal_exited()?;
        let settled = async {
            loop {
                let changed = agents.changed.notified();
                if !agents.state.lock().await.runs.contains_key("run-exit") {
                    break;
                }
                changed.await;
            }
        };
        let result = tokio::time::timeout(Duration::from_secs(5), settled).await;
        watcher.abort();
        result?;
        let state = persisted_state(root.path())?;
        let exit = &state
            .exits
            .iter()
            .find(|e| e.exit.run_id == "run-exit")
            .context("exit event not recorded")?
            .exit;
        assert_eq!(
            exit.reason,
            if cancelled {
                ExitReason::Cancelled
            } else {
                ExitReason::Exited
            }
        );
        assert_eq!(exit.exit_code, Some(0));
        assert!(exit.launched);
        assert!(exit.workspace.is_some());
    }
    Ok(())
}
