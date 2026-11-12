use super::*;
use std::sync::atomic::AtomicBool;

#[tokio::test]
async fn sustained_metadata_cannot_starve_native_frames_or_render_ticks() {
    let mock = Arc::new(MockDaemon::start("local-token").await);
    let host = DirectHost::start("epoch-1").await;
    mock.serve_direct_attach(host.attach_locator("terminal-b"));
    roster(
        &mock,
        vec![json!({"terminal_id":"terminal-b", "backend":"native",
        "state":"live", "attach":host.roster_attach("terminal-b")})],
    );
    mock.seed_workspace("project-1", &[(&["terminal-b"], "terminal-b")]);
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .unwrap();
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().unwrap();
    std::fs::write(home.path().join("bootstrap.yaml"), "api_key: local-token\n").unwrap();
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).unwrap();
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let driver = async {
        host.expect_attaches(1).await;
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        let producer = mock.clone();
        let flood = tokio::spawn(async move {
            let mut ready = Some(ready_tx);
            while !stopped.load(Ordering::Acquire) {
                for _ in 0..256 {
                    producer.send_event(json!({"type":"metadata"}));
                }
                if let Some(ready) = ready.take() {
                    let _ = ready.send(());
                }
                tokio::task::yield_now().await;
            }
        });
        ready_rx.await.unwrap();
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames.clone());
        probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
        host.wait_for("native key while metadata is ready", |seen| {
            seen.iter()
                .any(|message| matches!(message, ClientMessage::Input { data } if data == b"x"))
        })
        .await;
        host.to_client
            .send(frame_message("native-progress"))
            .unwrap();
        wait_within(
            Duration::from_millis(250),
            "native frame before metadata stops",
            || {
                frames
                    .lock()
                    .unwrap()
                    .last()
                    .is_some_and(|frame| frame.contains("native-progress"))
            },
        )
        .await;
        probe.draws_continue().await;
        stop.store(true, Ordering::Release);
        flood.await.unwrap();
        assert!(written(&mock, "terminal-b").is_empty());
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
    result.unwrap();
    Arc::try_unwrap(mock).ok().unwrap().shutdown().await;
}
