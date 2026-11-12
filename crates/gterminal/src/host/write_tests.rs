use super::*;
use crate::host::config::HostConfig;
use crate::protocol::RenderEncoding;
use tokio::sync::watch;
use tokio::time::timeout;

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn closing_during_capture_preserves_the_explicit_refusal() {
    let state = state();
    crate::host::state::insert_native_slot(&state, "retired", 24, 80).await;
    let _received = attach_accepting_child(&state, "retired").await;
    let (capture, slot) = {
        let mut inner = state.inner.lock().await;
        let identity = inner.by_host_id.remove("retired").unwrap();
        let slot = inner.terminals.remove(&identity).unwrap();
        let capture = slot
            .child
            .as_ref()
            .unwrap()
            .runtime
            .terminal_handle()
            .begin_history_capture()
            .unwrap();
        (capture, slot)
    };
    state.retain_exit_tail(Some(slot)).await;
    let response = state
        .snapshot(json!({"host_terminal_id":"retired"}).as_object().unwrap())
        .await;
    assert_eq!(response["error"], "capture_busy", "{response}");
    drop(capture);
}

#[cfg(feature = "vt-engine")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn held_terminal_control_work_allows_cross_pane_input_and_echo() {
    for operation in ["resize", "read", "scroll", "theme", "bind", "inherit"] {
        let state = state();
        for id in ["held", "typing"] {
            crate::host::state::insert_native_slot(&state, id, 24, 80).await;
        }
        let _held_rx = attach_accepting_child(&state, "held").await;
        let mut received = attach_accepting_child(&state, "typing").await;
        let (held_att, _) = state
            .attach(
                "held",
                None,
                crate::protocol::RenderEncoding::SemanticFrame,
                24,
                80,
            )
            .await
            .unwrap();
        let (typing_att, mailbox) = state
            .attach(
                "typing",
                None,
                crate::protocol::RenderEncoding::SemanticFrame,
                24,
                80,
            )
            .await
            .unwrap();
        let theme = crate::terminal_theme::ThemeDeclaration::from(
            &crate::terminal_theme::TerminalTheme::default(),
        );
        state
            .declare_terminal_theme(Some(held_att), theme.clone())
            .await
            .unwrap();
        let (release, thread, started) = {
            let inner = state.inner.lock().await;
            let runtime = &inner
                .terminals
                .get(inner.by_host_id.get("held").unwrap())
                .unwrap()
                .child
                .as_ref()
                .unwrap()
                .runtime;
            let started = runtime.test_frame_started();
            let (release, thread) = runtime.test_hold_terminal();
            (release, thread, started)
        };
        let pending = {
            let state = state.clone();
            tokio::spawn(async move {
                match operation {
                    "resize" => assert_eq!(
                        state
                            .resize(
                                json!({"host_terminal_id":"held", "rows":25,"cols":81})
                                    .as_object()
                                    .unwrap()
                            )
                            .await["ok"],
                        true
                    ),
                    "read" => {
                        state.read_text(held_att, 0, 0, 0, 1).await.unwrap();
                    }
                    "scroll" => {
                        state.set_scroll(held_att, 1).await.unwrap();
                    }
                    "theme" => {
                        state
                            .declare_terminal_theme(Some(held_att), theme)
                            .await
                            .unwrap();
                    }
                    "bind" => {
                        state
                            .bind_attachment(held_att, "client".into())
                            .await
                            .unwrap();
                    }
                    "inherit" => {
                        state.inherited_theme("held").await;
                    }
                    _ => unreachable!(),
                }
            })
        };
        tokio::time::timeout(Duration::from_secs(1), started)
            .await
            .unwrap()
            .unwrap();
        state
            .frame_input(Some(typing_att), NativeInput::Bytes(b"ECHO".to_vec()))
            .await
            .unwrap();
        assert_eq!(received.try_recv().unwrap().as_ref(), b"ECHO");
        {
            let inner = state.inner.lock().await;
            inner
                .terminals
                .get(inner.by_host_id.get("typing").unwrap())
                .unwrap()
                .child
                .as_ref()
                .unwrap()
                .runtime
                .test_feed_output(b"ECHO");
        }
        state.broadcast_frames().await;
        let echo = tokio::time::timeout(Duration::from_millis(250), mailbox.recv()).await;
        release.send(()).unwrap();
        thread.join().unwrap();
        pending.await.unwrap();
        let Some(crate::protocol::ServerMessage::Frame(frame)) = echo.expect(operation) else {
            panic!("missing {operation} frame")
        };
        let text: String = frame
            .cells
            .iter()
            .map(|cell| cell.symbol.as_str())
            .collect();
        assert!(text.contains("ECHO"), "{operation}");
    }
}

#[cfg(feature = "vt-engine")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn viewport_resync_invalidates_an_inflight_frame_even_at_the_same_size() {
    let state = state();
    crate::host::state::insert_native_slot(&state, "pane", 24, 80).await;
    let _received = attach_accepting_child(&state, "pane").await;
    let (attachment, mailbox) = state
        .attach(
            "pane",
            None,
            crate::protocol::RenderEncoding::SemanticFrame,
            24,
            80,
        )
        .await
        .unwrap();
    state.broadcast_frames().await;
    tokio::time::timeout(Duration::from_secs(1), mailbox.recv())
        .await
        .unwrap()
        .unwrap();
    mailbox.note_drain();
    let (release, thread, started) = {
        let inner = state.inner.lock().await;
        let runtime = &inner
            .terminals
            .get(inner.by_host_id.get("pane").unwrap())
            .unwrap()
            .child
            .as_ref()
            .unwrap()
            .runtime;
        runtime.test_feed_output(b"changed");
        let started = runtime.test_frame_started();
        let (release, thread) = runtime.test_hold_terminal();
        (release, thread, started)
    };
    state.broadcast_frames().await;
    started.await.unwrap();
    state.set_viewport(attachment, 24, 80).await.unwrap();
    release.send(()).unwrap();
    thread.join().unwrap();
    tokio::time::timeout(Duration::from_secs(1), async {
        while state.frame_build.lock().await.contains(&attachment) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(
        mailbox.try_pop().is_none(),
        "stale frame published after viewport resync"
    );
    assert!(
        state
            .inner
            .lock()
            .await
            .attachments
            .get(&attachment)
            .unwrap()
            .desynced
    );
    state.broadcast_frames().await;
    assert!(tokio::time::timeout(Duration::from_secs(1), mailbox.recv())
        .await
        .unwrap()
        .is_some());
}

fn state() -> std::sync::Arc<HostState> {
    let (shutdown, _) = watch::channel(false);
    HostState::new(
        HostConfig::default(),
        "control".to_string(),
        "epoch".to_string(),
        crate::host::image::PinnedImage::for_tests(),
        1,
        shutdown,
    )
}

fn target(recipient_id: &str, terminal_id: &str) -> Value {
    json!({
        "recipient_id": recipient_id,
        "host_terminal_id": terminal_id,
        "operations": [
            {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 0},
        ],
    })
}

async fn remove_terminal(state: &HostState, host_terminal_id: &str) {
    let mut inner = state.inner.lock().await;
    let identity = inner
        .by_host_id
        .remove(host_terminal_id)
        .expect("terminal identity");
    inner.terminals.remove(&identity).expect("terminal slot");
}

#[cfg(feature = "vt-engine")]
async fn attach_accepting_child(
    state: &HostState,
    host_terminal_id: &str,
) -> tokio::sync::mpsc::Receiver<bytes::Bytes> {
    let (runtime, received) = crate::pane::PaneRuntime::test_with_channel(80, 24);
    let child = crate::host::spawn::PreparedChild::from_test_runtime(runtime);
    let mut inner = state.inner.lock().await;
    let identity = inner
        .by_host_id
        .get(host_terminal_id)
        .expect("terminal identity")
        .clone();
    inner
        .terminals
        .get_mut(&identity)
        .expect("terminal slot")
        .child = Some(child);
    received
}

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn native_write_refuses_enter_when_compact_is_queued() {
    let state = state();
    crate::host::state::insert_native_slot(&state, "ht-1", 24, 80).await;
    let mut received = attach_accepting_child(&state, "ht-1").await;
    for text in ["/compact", "a", "b", "c"] {
        let request = json!({"host_terminal_id": "ht-1", "kind": "text",
                "data": base64::engine::general_purpose::STANDARD.encode(text)});
        assert_eq!(
            state.write(request.as_object().unwrap()).await["written"],
            true
        );
    }
    let request = json!({"host_terminal_id": "ht-1", "kind": "key",
            "data": base64::engine::general_purpose::STANDARD.encode("enter")});
    let response = state.write(request.as_object().unwrap()).await;
    assert_eq!(
        response,
        json!({"ok": false, "written": false,
            "error": "pty_busy", "code": "pty_busy", "stage": "none"})
    );
    assert_eq!(received.try_recv().unwrap().as_ref(), b"/compact");
    for text in [b"a", b"b", b"c"] {
        assert_eq!(received.try_recv().unwrap().as_ref(), text);
    }
    assert!(received.try_recv().is_err());
}

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn native_write_refuses_closed_or_missing_writer() {
    for closed in [false, true] {
        for kind in ["text", "key", "paste"] {
            let state = state();
            crate::host::state::insert_native_slot(&state, "ht-1", 24, 80).await;
            if closed {
                drop(attach_accepting_child(&state, "ht-1").await);
            }
            let request = json!({"host_terminal_id": "ht-1", "kind": kind,
                    "data": base64::engine::general_purpose::STANDARD.encode("enter")});
            let response = state.write(request.as_object().unwrap()).await;
            assert_eq!(
                response,
                json!({"ok": false, "written": false,
                    "error": "terminal_gone", "code": "terminal_gone", "stage": "none"})
            );
        }
    }
}

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn native_write_accepts_text_key_and_paste() {
    for (kind, payload, expected) in [
        ("text", "/compact", &b"/compact"[..]),
        ("key", "enter", &b"\r"[..]),
        ("paste", "hello", &b"hello"[..]),
    ] {
        let state = state();
        crate::host::state::insert_native_slot(&state, "ht-1", 24, 80).await;
        let mut received = attach_accepting_child(&state, "ht-1").await;
        let request = json!({"host_terminal_id": "ht-1", "kind": kind,
                "data": base64::engine::general_purpose::STANDARD.encode(payload)});
        assert_eq!(
            state.write(request.as_object().unwrap()).await,
            json!({"ok": true, "written": true})
        );
        assert_eq!(received.try_recv().unwrap().as_ref(), expected);
        assert!(received.try_recv().is_err());
    }
}

#[cfg(feature = "vt-engine")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn held_terminal_capture_does_not_block_native_paste() {
    let state = state();
    crate::host::state::insert_native_slot(&state, "ht-1", 24, 80).await;
    let mut received = attach_accepting_child(&state, "ht-1").await;
    let (attachment, _) = state
        .attach(
            "ht-1",
            None,
            crate::protocol::RenderEncoding::SemanticFrame,
            24,
            80,
        )
        .await
        .unwrap();
    let (release, thread) = {
        let inner = state.inner.lock().await;
        let identity = inner.by_host_id.get("ht-1").unwrap();
        inner
            .terminals
            .get(identity)
            .unwrap()
            .child
            .as_ref()
            .unwrap()
            .runtime
            .test_hold_terminal()
    };
    let writer = {
        let state = state.clone();
        tokio::spawn(async move {
            state
                .frame_input(Some(attachment), NativeInput::Paste("hello\n".into()))
                .await
        })
    };
    let delivered = tokio::time::timeout(Duration::from_millis(150), received.recv()).await;
    // Always release the barrier before asserting, including on RED.
    release.send(()).unwrap();
    thread.join().unwrap();
    assert_eq!(writer.await.unwrap(), Ok(()));
    assert_eq!(
        delivered
            .expect("paste must reach PTY while terminal capture is held")
            .unwrap()
            .as_ref(),
        b"hello\n"
    );
    assert!(received.try_recv().is_err());
}

#[cfg(feature = "vt-engine")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn held_frame_build_does_not_block_another_panes_input_and_echo() {
    let state = state();
    for id in ["held", "typing"] {
        crate::host::state::insert_native_slot(&state, id, 24, 80).await;
    }
    let _held_rx = attach_accepting_child(&state, "held").await;
    let mut received = attach_accepting_child(&state, "typing").await;
    state
        .attach(
            "held",
            None,
            crate::protocol::RenderEncoding::SemanticFrame,
            24,
            80,
        )
        .await
        .unwrap();
    let (attachment, mailbox) = state
        .attach(
            "typing",
            None,
            crate::protocol::RenderEncoding::SemanticFrame,
            24,
            80,
        )
        .await
        .unwrap();
    let (release, thread, started) = {
        let inner = state.inner.lock().await;
        let identity = inner.by_host_id.get("held").unwrap();
        let runtime = &inner
            .terminals
            .get(identity)
            .unwrap()
            .child
            .as_ref()
            .unwrap()
            .runtime;
        let started = runtime.test_frame_started();
        let (release, thread) = runtime.test_hold_terminal();
        (release, thread, started)
    };
    let broadcast = {
        let state = state.clone();
        tokio::spawn(async move { state.broadcast_frames().await })
    };
    started.await.unwrap();
    let writer = {
        let state = state.clone();
        tokio::spawn(async move {
            state
                .frame_input(Some(attachment), NativeInput::Bytes(b"x".to_vec()))
                .await
        })
    };
    let delivered = tokio::time::timeout(Duration::from_millis(150), received.recv()).await;
    tokio::time::timeout(Duration::from_millis(250), mailbox.recv())
        .await
        .unwrap();
    {
        let inner = state.inner.lock().await;
        let identity = inner.by_host_id.get("typing").unwrap();
        inner
            .terminals
            .get(identity)
            .unwrap()
            .child
            .as_ref()
            .unwrap()
            .runtime
            .test_feed_output(b"ECHO");
    }
    let next_broadcast = {
        let state = state.clone();
        tokio::spawn(async move { state.broadcast_frames().await })
    };
    let echo = tokio::time::timeout(Duration::from_millis(250), mailbox.recv()).await;
    release.send(()).unwrap();
    thread.join().unwrap();
    assert_eq!(writer.await.unwrap(), Ok(()));
    broadcast.await.unwrap();
    next_broadcast.await.unwrap();
    let crate::protocol::ServerMessage::Frame(frame) = echo
        .expect("other pane echo before held frame release")
        .unwrap()
    else {
        panic!("expected echo frame")
    };
    let text: String = frame
        .cells
        .iter()
        .map(|cell| cell.symbol.as_str())
        .collect();
    assert!(text.contains("ECHO"));
    assert_eq!(
        delivered
            .expect("frame work must release the host lock")
            .unwrap()
            .as_ref(),
        b"x"
    );
}

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn bounded_history_reports_unknown_full_history_counts() {
    let state = state();
    crate::host::state::insert_native_slot(&state, "ht-1", 2, 80).await;
    let _received = attach_accepting_child(&state, "ht-1").await;
    {
        let inner = state.inner.lock().await;
        let identity = inner.by_host_id.get("ht-1").unwrap();
        inner
            .terminals
            .get(identity)
            .unwrap()
            .child
            .as_ref()
            .unwrap()
            .runtime
            .test_feed_output(b"old\r\nnew\r\ntail");
    }
    let reply = state
        .snapshot(
            json!({"host_terminal_id":"ht-1", "max_lines":1,
            "max_bytes":128})
            .as_object()
            .unwrap(),
        )
        .await;
    assert_eq!(reply["ok"], true);
    assert_eq!(reply["text"], "tail");
    assert_eq!(reply["truncated"], true);
    assert!(reply["total_bytes"].is_null(), "{reply}");
    assert!(reply["dropped_bytes"].is_null(), "{reply}");
}

#[tokio::test]
async fn batch_returns_one_ordered_result_per_target() {
    let value = json!({
        "targets": [target("r1", "t1"), target("r2", "t2"), target("r3", "t3")],
    });
    let response = state().write_batch(value.as_object().unwrap()).await;

    assert_eq!(response["ok"], true);
    assert_eq!(response["results"].as_array().unwrap().len(), 3);
    assert_eq!(response["results"][0]["recipient_id"], "r1");
    assert_eq!(response["results"][1]["recipient_id"], "r2");
    assert_eq!(response["results"][2]["recipient_id"], "r3");
    assert!(response["results"]
        .as_array()
        .unwrap()
        .iter()
        .all(|result| result["error"] == "not_found" && result["stage"] == "none"));
}

#[tokio::test]
async fn batch_rejects_global_and_per_target_limits_before_writes() {
    let targets: Vec<_> = (0..=MAX_WRITE_BATCH_TARGETS)
        .map(|index| target(&format!("r{index}"), &format!("t{index}")))
        .collect();
    let too_many = json!({"targets": targets});
    let response = state().write_batch(too_many.as_object().unwrap()).await;
    assert_eq!(response["error"], "too_many_targets");

    let operations: Vec<_> = (0..=MAX_WRITE_BATCH_OPERATIONS_PER_TARGET)
        .map(|_| json!({"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 0}))
        .collect();
    let invalid_target = json!({
        "targets": [{
            "recipient_id": "r1",
            "host_terminal_id": "t1",
            "operations": operations,
        }],
    });
    let response = state()
        .write_batch(invalid_target.as_object().unwrap())
        .await;
    assert_eq!(response["ok"], true);
    assert_eq!(response["results"][0]["error"], "too_many_operations");
    assert_eq!(response["results"][0]["stage"], "none");

    let delayed = json!({
        "targets": [{
            "recipient_id": "r1",
            "host_terminal_id": "t1",
            "operations": (0..6).map(|_| json!({
                "kind": "text",
                "encoding": "utf8-b64",
                "data": "eA==",
                "delay_ms": 1_000,
            })).collect::<Vec<_>>(),
        }],
    });
    let response = state().write_batch(delayed.as_object().unwrap()).await;
    assert_eq!(response["results"][0]["error"], "invalid_delay");
    assert_eq!(response["results"][0]["stage"], "none");
}

#[tokio::test]
async fn native_input_is_refused_when_the_slot_owns_no_pty() {
    let state = state();
    crate::host::state::insert_native_slot(&state, "ht-1", 24, 80).await;
    let mut inner = state.inner.lock().await;
    let slot = native_slot_mut(&mut inner, "ht-1").expect("native slot");

    // Answering Ok here would report a delivery to a terminal that owns no
    // writer, and the frame stream would emit `input_activity` for it.
    assert_eq!(
        deliver_native(slot, NativeInput::Bytes(b"x".to_vec())),
        Err("terminal_gone"),
    );
    assert_eq!(
        deliver_native(slot, NativeInput::Paste("x".to_owned())),
        Err("terminal_gone"),
    );
}

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn a_paste_submits_only_when_the_child_reads_it_unbracketed() {
    for (child_output, submits) in [(&b""[..], true), (b"\x1b[?2004h", false)] {
        let state = state();
        crate::host::state::insert_native_slot(&state, "ht-1", 24, 80).await;
        let (runtime, _received) =
            crate::pane::PaneRuntime::test_with_channel_and_scrollback_limits(
                80,
                24,
                crate::ghostty::ScrollbackLimits { bytes: 0, lines: 0 },
                child_output,
                4,
            );
        let mut inner = state.inner.lock().await;
        let slot = native_slot_mut(&mut inner, "ht-1").expect("native slot");
        slot.child = Some(crate::host::spawn::PreparedChild::from_test_runtime(
            runtime,
        ));

        assert_eq!(
            NativeInput::Paste("ls\r".to_owned()).classify(slot),
            (InputEffect::Edit, submits)
        );
        assert_eq!(
            NativeInput::Paste("ls".to_owned()).classify(slot),
            (InputEffect::Edit, false)
        );
        assert_eq!(
            NativeInput::Bytes(b"\r".to_vec()).classify(slot),
            (InputEffect::Edit, true)
        );
    }
}

#[tokio::test]
async fn batch_delay_does_not_block_input_on_another_terminal() {
    let state = state();
    crate::host::state::insert_native_slot(&state, "batch", 24, 80).await;
    crate::host::state::insert_native_slot(&state, "interactive", 24, 80).await;
    let (attachment_id, _mailbox) = state
        .attach("interactive", None, RenderEncoding::SemanticFrame, 24, 80)
        .await
        .expect("attach interactive terminal");
    state
        .bind_attachment(attachment_id, "client".to_owned())
        .await
        .expect("bind interactive terminal");

    let batch = json!({
        "targets": [{
            "recipient_id": "recipient",
            "host_terminal_id": "batch",
            "operations": [
                {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 1_000},
                {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 1_000},
            ],
        }],
    });
    let batch_state = state.clone();
    let batch_task = tokio::spawn(async move {
        batch_state
            .write_batch(batch.as_object().expect("batch object"))
            .await
    });
    tokio::task::yield_now().await;

    let framed = timeout(
        Duration::from_millis(250),
        state.frame_input(Some(attachment_id), NativeInput::Bytes(b"x".to_vec())),
    )
    .await
    .expect("frame_input blocked behind batch delay");
    assert_eq!(framed, Err("terminal_gone"));

    batch_task.abort();
    assert!(batch_task
        .await
        .expect_err("batch task should be cancelled")
        .is_cancelled());
}

#[tokio::test(start_paused = true)]
async fn batch_reports_removed_targets_and_continues_later_operations() {
    let state = state();
    crate::host::state::insert_native_slot(&state, "removed-first", 24, 80).await;
    crate::host::state::insert_native_slot(&state, "removed-later", 24, 80).await;
    #[cfg(feature = "vt-engine")]
    let (_first_input, _later_input) = {
        let first = attach_accepting_child(&state, "removed-first").await;
        let later = attach_accepting_child(&state, "removed-later").await;
        (first, later)
    };
    let batch = json!({
        "targets": [
            {
                "recipient_id": "first",
                "host_terminal_id": "removed-first",
                "operations": [
                    {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 0},
                    {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 100},
                ],
            },
            {
                "recipient_id": "later",
                "host_terminal_id": "removed-later",
                "operations": [
                    {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 500},
                ],
            },
        ],
    });
    let batch_state = state.clone();
    let batch_task = tokio::spawn(async move {
        batch_state
            .write_batch(batch.as_object().expect("batch object"))
            .await
    });
    tokio::task::yield_now().await;
    tokio::time::advance(Duration::from_millis(1)).await;
    tokio::task::yield_now().await;

    remove_terminal(&state, "removed-first").await;
    tokio::time::advance(Duration::from_millis(249)).await;
    tokio::task::yield_now().await;
    remove_terminal(&state, "removed-later").await;
    tokio::time::advance(Duration::from_millis(250)).await;

    let response = batch_task.await.expect("batch task");
    assert_eq!(response["results"][0]["error"], "not_found");
    assert_eq!(response["results"][0]["stage"], "partial");
    assert_eq!(response["results"][1]["error"], "not_found");
    assert_eq!(response["results"][1]["stage"], "none");
}
#[tokio::test]
async fn attachment_bind_respects_the_upgrade_mutation_fence() {
    let state = state();
    crate::host::state::insert_native_slot(&state, "pane", 24, 80).await;
    let (attachment, _mailbox) = state
        .attach(
            "pane",
            None,
            crate::protocol::RenderEncoding::SemanticFrame,
            24,
            80,
        )
        .await
        .unwrap();
    let _upgrade = state.mutation_gate.write().await;
    assert_eq!(
        state.bind_attachment(attachment, "client".into()).await,
        Err("host_upgrading")
    );
    assert!(state
        .inner
        .lock()
        .await
        .attachments
        .get(&attachment)
        .unwrap()
        .client_attachment_id
        .is_none());
}
#[cfg(feature = "vt-engine")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn same_terminal_title_change_publishes_to_every_attachment_in_one_pass() {
    for encoding in [
        crate::protocol::RenderEncoding::SemanticFrame,
        crate::protocol::RenderEncoding::TerminalAnsi,
    ] {
        let state = state();
        crate::host::state::insert_native_slot(&state, "pane", 24, 80).await;
        let _received = attach_accepting_child(&state, "pane").await;
        let (_, first) = state.attach("pane", None, encoding, 24, 80).await.unwrap();
        let (_, second) = state.attach("pane", None, encoding, 24, 80).await.unwrap();
        let (release, thread) = {
            let inner = state.inner.lock().await;
            let runtime = &inner
                .terminals
                .get(inner.by_host_id.get("pane").unwrap())
                .unwrap()
                .child
                .as_ref()
                .unwrap()
                .runtime;
            runtime.test_feed_output(b"\x1b]2;new title\x07visible");
            runtime.test_hold_terminal()
        };
        state.broadcast_frames().await;
        assert_eq!(state.frame_build.lock().await.len(), 2);
        release.send(()).unwrap();
        thread.join().unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            while !state.frame_build.lock().await.is_empty() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert!(
            first.try_pop().is_some(),
            "first viewer lost its title-change frame"
        );
        assert!(
            second.try_pop().is_some(),
            "second viewer lost its title-change frame"
        );
    }
}
