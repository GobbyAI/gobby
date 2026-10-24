use super::*;

#[test]
fn api_key_ignores_the_socket_directory() {
    let root = tempfile::tempdir().expect("tempdir");
    let socket_dir = root.path().join("gterm-host");
    let gobby_home = root.path().join("gobby-home");
    fs::create_dir_all(&socket_dir).expect("socket dir");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    fs::write(socket_dir.join("bootstrap.yaml"), "api_key: socket-key\n").expect("socket key");
    fs::write(gobby_home.join("bootstrap.yaml"), "api_key: home-key\n").expect("home key");

    assert_eq!(
        read_api_key_from(Some(gobby_home.as_path())),
        Some("home-key".into())
    );
}

#[test]
fn api_key_reads_bootstrap_fresh() {
    let root = tempfile::tempdir().expect("tempdir");
    let gobby_home = root.path().join("gobby-home");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    for key in ["first-key", "second-key"] {
        fs::write(
            gobby_home.join("bootstrap.yaml"),
            format!("api_key: ' {key} '\n"),
        )
        .expect("home key");
        assert_eq!(
            read_api_key_from(Some(gobby_home.as_path())),
            Some(key.into())
        );
    }
}

#[test]
fn api_key_is_absent_without_a_usable_bootstrap_key() {
    let root = tempfile::tempdir().expect("tempdir");
    let gobby_home = root.path().join("gobby-home");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    assert_eq!(read_api_key_from(None), None);
    assert_eq!(read_api_key_from(Some(gobby_home.as_path())), None);
    for content in [
        "",
        "api_key: ''\n",
        "api_key: 123\n",
        "api_key: [\n",
        "api_key: null\n",
    ] {
        fs::write(gobby_home.join("bootstrap.yaml"), content).expect("bootstrap");
        assert_eq!(read_api_key_from(Some(gobby_home.as_path())), None);
    }
}

#[cfg(all(unix, feature = "vt-engine"))]
async fn frame_grant_fixture() -> (
    Arc<state::HostState>,
    tokio::sync::mpsc::Receiver<bytes::Bytes>,
    u64,
    u64,
) {
    use crate::protocol::RenderEncoding;
    let (shutdown, _) = tokio::sync::watch::channel(false);
    let state = state::HostState::new(
        config::HostConfig::default(),
        "control".into(),
        "epoch".into(),
        image::PinnedImage::for_tests(),
        1,
        shutdown,
    );
    state::insert_native_slot(&state, "native", 24, 80).await;
    let (runtime, received) = crate::pane::PaneRuntime::test_with_channel(80, 24);
    {
        let mut inner = state.inner.lock().await;
        let identity = inner.by_host_id["native"].clone();
        inner.terminals.get_mut(&identity).unwrap().child =
            Some(spawn::PreparedChild::from_test_runtime(runtime));
    }
    let (a, _) = state
        .attach("native", None, RenderEncoding::TerminalAnsi, 24, 80)
        .await
        .unwrap();
    let (b, _) = state
        .attach("native", None, RenderEncoding::TerminalAnsi, 24, 80)
        .await
        .unwrap();
    (state, received, a, b)
}

#[cfg(all(unix, feature = "vt-engine"))]
#[tokio::test]
async fn host_grant_rejects_holder_name_theft_and_revokes_the_old_stream() {
    use serde_json::json;
    use write::NativeInput;
    let (state, mut received, a, b) = frame_grant_fixture().await;
    let granted = state
        .grant_input(
            json!({"host_terminal_id": "native", "attachment_id": "holder-a",
            "host_attachment_id": a})
            .as_object()
            .unwrap(),
        )
        .await;
    assert_eq!(granted["granted"], true);
    // B races the legitimate client's first bind, after the trusted grant to A.
    let stolen = state.bind_attachment(b, "holder-a".into()).await;
    assert_eq!(stolen, Err("attachment_already_bound"));
    let cursor = state.events.cursor().await;
    for input in [
        NativeInput::Bytes(b"attack".to_vec()),
        NativeInput::Paste("attack".into()),
    ] {
        assert_eq!(
            state.frame_input(Some(b), input).await,
            Err("input_not_granted")
        );
    }
    assert!(received.try_recv().is_err());
    assert_eq!(state.events.cursor().await, cursor);
    state
        .frame_input(Some(a), NativeInput::Bytes(b"owner".to_vec()))
        .await
        .unwrap();
    assert_eq!(received.try_recv().unwrap().as_ref(), b"owner");
    state.bind_attachment(a, "holder-a".into()).await.unwrap();
    assert_eq!(
        state.bind_attachment(b, "holder-a".into()).await,
        Err("attachment_already_bound")
    );
    let moved = state
        .grant_input(
            json!({"host_terminal_id": "native", "attachment_id": "holder-b",
            "host_attachment_id": b})
            .as_object()
            .unwrap(),
        )
        .await;
    assert_eq!(moved["previous"], "holder-a");
    let cursor = state.events.cursor().await;
    for input in [
        NativeInput::Bytes(b"stale".to_vec()),
        NativeInput::Paste("stale".into()),
    ] {
        assert_eq!(
            state.frame_input(Some(a), input).await,
            Err("input_not_granted")
        );
    }
    assert!(received.try_recv().is_err());
    assert_eq!(state.events.cursor().await, cursor);
    state
        .frame_input(Some(b), NativeInput::Paste("new owner".into()))
        .await
        .unwrap();
    assert_eq!(received.try_recv().unwrap().as_ref(), b"new owner");
}

#[cfg(all(unix, feature = "vt-engine"))]
#[tokio::test]
async fn holder_theme_name_stays_reserved_when_its_stream_changes_labels() {
    let (state, _received, a, b) = frame_grant_fixture().await;
    let grant = state
        .grant_input(
            serde_json::json!({
                "host_terminal_id": "native", "attachment_id": "holder", "host_attachment_id": a,
            })
            .as_object()
            .unwrap(),
        )
        .await;
    assert_eq!(grant["granted"], true);
    state
        .bind_attachment(a, "another-label".into())
        .await
        .unwrap();
    assert_eq!(
        state.bind_attachment(b, "holder".into()).await,
        Err("attachment_already_bound")
    );
}
