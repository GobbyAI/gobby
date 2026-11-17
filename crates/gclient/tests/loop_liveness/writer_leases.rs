use super::*;

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
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames).with_native_hosts(&_home);
        probe.ready(0).await;
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
