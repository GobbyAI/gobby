use super::*;

#[tokio::test]
async fn direct_tmux_keys_are_admitted_after_the_attach_claim() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.set_attach_backend("terminal-b", "tmux");
    let host = StallingHost::start().await;
    let mut attach = host.roster_attach("terminal-b");
    attach["backend"] = json!("tmux");
    attach["socket_path"] = json!("/test-owned-tmux.sock");
    attach["pane_id"] = json!("%1");
    attach["server_pid"] = json!(42);
    attach["server_start_time"] = json!(123);
    let mut locator = host.attach_locator("terminal-b");
    locator["pane"] = json!({
        "socket_path": "/test-owned-tmux.sock", "pane_id": "%1",
        "server_pid": 42, "server_start_time": 123,
    });
    mock.serve_direct_attach(locator);
    mock.enqueue("GET", "/api/terminals?", 200, json!({
        "items": [{"terminal_id": "terminal-b", "backend": "tmux", "state": "live", "attach": attach}],
        "next_cursor": null, "snapshot": {"daemon_epoch": "epoch-1", "seq": 1},
    }));
    mock.seed_workspace("project-1", &[(&["terminal-b"], "terminal-b")]);
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("home");
    std::fs::write(home.path().join("bootstrap.yaml"), "api_key: local-token\n").expect("key");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_focus", 1).await;
        probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_until("tmux daemon write", || {
            written(&mock, "terminal-b") == ["x"]
        })
        .await;
        drop(input_tx);
    };
    let mut switch = TerminalGuard::recording().0;
    let (result, ()) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("loop exits");
    let pane = workspace.pane(workspace.pane_for_terminal("terminal-b").expect("pane"));
    assert_eq!(pane.transport(), Some(Transport::Direct));
    let requests: Vec<_> = mock.requests().into_iter().filter_map(|r| r.body).collect();
    // Replay the client's actual wire order through the daemon's production admission.
    // The frame host above is a protocol fixture; it cannot grant daemon write authority.
    mock_daemon::lease_admission::assert_tmux_write_admission(&requests, "terminal-b");
    mock.shutdown().await;
}

#[tokio::test]
async fn direct_native_attach_types_without_claiming_a_daemon_lease() {
    let mock = MockDaemon::start("local-token").await;
    let mut host = StallingHost::start().await;
    let (mut workspace, _home) = two_pane_workspace(&mock, Some(&host)).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
        host.wait_for_input(b"x").await;
        drop(input_tx);
    };
    let mut switch = TerminalGuard::recording().0;
    let (result, ()) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("loop exits");
    assert!(websocket_requests(&mock, "terminal_take_control")
        .iter()
        .all(|request| request["terminal_id"] != "terminal-b"));
    assert!(written(&mock, "terminal-b").is_empty());
    mock.shutdown().await;
}
