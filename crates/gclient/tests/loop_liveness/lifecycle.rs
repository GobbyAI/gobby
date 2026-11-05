//! A4b: user actions must yield while their daemon mutations are outstanding.

use super::*;
use gobby_client::app::{build_menu, sync_live_chrome, ArrangeTarget, ContextMenuKind, Submenu};
use gobby_client::ui::dialogs::Dialog;
use gobby_client::ui::keymap::{Keymap, HERDR_PREFIX};
use gobby_client::ui::Mode;

const FIRST_SHELL: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SECOND_SHELL: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

fn operations(mock: &MockDaemon, op: &str) -> Vec<Value> {
    mock.workspace_requests()
        .into_iter()
        .filter(|request| request["op"] == op)
        .collect()
}

fn action_chrome() -> Chrome {
    let mut chrome = Chrome::dark();
    chrome.prefs.confirm_close = false;
    chrome.keymap = Keymap::from_toml(
        "[bindings]\nnew_terminal = \"prefix+i\"\nnew_tab = \"prefix+c\"\n",
        HERDR_PREFIX,
    )
    .expect("lifecycle test bindings");
    chrome
}

async fn chord(probe: &LivenessProbe<'_>, key: char) {
    probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
    probe.key(KeyCode::Char(key), KeyModifiers::NONE).await;
}

/// Wait until the host receives this keypress. `expect_input` also counts the
/// attach's bind, which arrives before any key, so it can pass while the key
/// still waits on the host grant.
async fn wait_for_typed_at_host(host: &DirectHost, data: &[u8]) {
    wait_until("typed key at the host", || {
        host.drain()
            .iter()
            .any(|message| matches!(message, ClientMessage::Input { data: sent } if sent == data))
    })
    .await;
}

/// Take a bounded observation while the mutation is held, then release it.
/// Fail on the missing rendered content/input, rather than on a timeout.
async fn assert_progress_while_held(probe: &LivenessProbe<'_>, terminal: &str, release: &Notify) {
    let attachment = attachment_for(probe.mock, terminal);
    let before = probe.draws.load(Ordering::SeqCst);
    let writes_before = written(probe.mock, terminal).len();
    let tag = format!("lifecycle-held-{before}");
    for _ in 0..PROBE_FRAMES {
        probe
            .mock
            .send_event_and_wait(json!({
                "type": "terminal_frame", "terminal_id": terminal,
                "attachment_id": attachment, "encoding": "bincode-b64",
                "payload": encoded_frame(&tag),
            }))
            .await;
        tokio::time::sleep(RENDER_TICK).await;
    }
    probe.key(KeyCode::Char('v'), KeyModifiers::NONE).await;
    // A bounded sampling window lets the runtime deliver the frames and key.
    // The hold remains closed throughout; it is independent of request deadlines.
    tokio::time::sleep(Duration::from_millis(250)).await;
    let rendered = probe
        .frames
        .lock()
        .expect("frames")
        .last()
        .is_some_and(|frame| frame.contains(&tag));
    let drew = probe.draws.load(Ordering::SeqCst) > before;
    let typed = written(probe.mock, terminal)
        .get(writes_before..)
        .is_some_and(|writes| writes.iter().any(|text| text == "v"));
    release.notify_one();
    assert!(
        rendered,
        "a held lifecycle mutation must not stop frame ingest/rendering"
    );
    assert!(
        drew,
        "render ticks must advance while the lifecycle mutation is held"
    );
    assert!(
        typed,
        "unaffected pane input must advance while the lifecycle mutation is held"
    );
}

async fn assert_direct_progress_while_held(
    probe: &LivenessProbe<'_>,
    host: &DirectHost,
    release: &Notify,
) {
    host.drain();
    let before = probe.draws.load(Ordering::SeqCst);
    let tag = format!("direct-held-{before}");
    for _ in 0..PROBE_FRAMES {
        host.to_client
            .send(frame_message(&tag))
            .expect("direct frame");
        tokio::time::sleep(RENDER_TICK).await;
    }
    probe.key(KeyCode::Char('v'), KeyModifiers::NONE).await;
    tokio::time::sleep(Duration::from_millis(250)).await;
    let rendered = probe
        .frames
        .lock()
        .expect("frames")
        .last()
        .is_some_and(|frame| frame.contains(&tag));
    let typed = host
        .drain()
        .iter()
        .any(|message| matches!(message, ClientMessage::Input { data } if data == b"v"));
    let drew = probe.draws.load(Ordering::SeqCst) > before;
    release.notify_one();
    assert!(
        rendered,
        "held lifecycle mutation must keep direct frames rendering"
    );
    assert!(
        drew,
        "held lifecycle mutation must keep render ticks advancing"
    );
    assert!(
        typed,
        "held lifecycle mutation must keep direct input flowing"
    );
}

#[tokio::test]
async fn split_right_with_a_held_control_or_create_reply_keeps_the_window_live() {
    for held_kind in ["workspace_op", "terminal_release_control"] {
        let mock = MockDaemon::start("local-token").await;
        mock.queue_owned_terminal_id(FIRST_SHELL);
        let mut host = DirectHost::start("epoch-1").await;
        // A gterm host serves its control socket, so the host grant a keypress
        // asks for resolves here. Without it every grant fails to connect, and
        // whether the queued key survives then depends on which of that error
        // and the take's answer lands first.
        host.serve_gterm_control("control-token");
        mock.seed_workspace(
            "project-1",
            &[(&["terminal-a", "terminal-b"], "terminal-b")],
        );
        let (mut workspace, _home) = mock_daemon::live_workspace_on_direct_host_terminals(
            &mock,
            &host,
            &["terminal-a", "terminal-b"],
        )
        .await;
        // Consume the helper's second initial roster before arranging the
        // roster that follows this test's new shell.
        workspace
            .fetch_roster()
            .await
            .expect("initial direct roster");
        mock.enqueue("GET", "/api/terminals?", 200, json!({
            "items": [
                {"terminal_id": "terminal-a", "backend": "native", "state": "live", "attach": host.roster_attach("terminal-a")},
                {"terminal_id": "terminal-b", "backend": "native", "state": "live", "attach": host.roster_attach("terminal-b")},
                {"terminal_id": FIRST_SHELL, "backend": "native", "state": "live"},
            ],
            "next_cursor": null, "snapshot": {"daemon_epoch": "epoch-1", "seq": 2},
        }));
        let mut chrome = action_chrome();
        let (backend, draws, frames) = DrawRecorder::new(96, 30);
        let mut terminal = Terminal::new(backend).expect("terminal");
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
            wait_for_replies(&mock, "terminal_take_control", 1).await;
            probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
            wait_for_typed_at_host(&host, b"x").await;
            let release = mock.hold_ws(held_kind, |request| {
                request["op"] == "pane.split" || request["terminal_id"] == "terminal-b"
            });
            if held_kind == "terminal_release_control" {
                probe.next_pane().await;
                wait_for_websocket_requests(&mock, held_kind, 1).await;
                // The split below commits and focuses its new shell, which
                // the direct host does not serve: probe while the release is held.
                assert_direct_progress_while_held(&probe, &host, &release).await;
            }
            chord(&probe, 'i').await;
            wait_until("split request", || {
                operations(&mock, "pane.split").len() == 1
            })
            .await;
            if held_kind == "workspace_op" {
                assert!(
                    attach_requests(&mock, FIRST_SHELL).is_empty(),
                    "no placement before commit"
                );
                assert_direct_progress_while_held(&probe, &host, &release).await;
            }
            wait_until("new shell viewport", || {
                websocket_requests(&mock, "terminal_set_viewport")
                    .iter()
                    .any(|request| request["terminal_id"] == FIRST_SHELL)
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
        result.expect("split loop");
        let pane = workspace
            .pane_for_terminal(FIRST_SHELL)
            .expect("created terminal");
        assert!(chrome
            .tabs()
            .tabs
            .iter()
            .any(|tab| tab.slots.values().any(|id| *id == pane)));
        assert_eq!(
            chrome.focused_pane(),
            Some(pane),
            "the committed split takes focus"
        );
        assert_eq!(operations(&mock, "pane.split").len(), 1);
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn two_new_tabs_before_the_first_create_lands_place_both() {
    let mock = MockDaemon::start("local-token").await;
    mock.queue_owned_terminal_id(FIRST_SHELL);
    mock.queue_owned_terminal_id(SECOND_SHELL);
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let mut chrome = action_chrome();
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("terminal");
    let (input_tx, input_rx) = mpsc::channel(16);
    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        let release = mock.hold_ws("workspace_op", |request| request["op"] == "tab.create");
        chord(&probe, 'c').await;
        chord(&probe, 'i').await;
        wait_until("first create", || {
            operations(&mock, "tab.create").len() == 1
        })
        .await;
        chord(&probe, 'c').await;
        chord(&probe, 'i').await;
        let _second_request = timeout(LIVENESS_DEADLINE, async {
            while operations(&mock, "tab.create").len() < 2 {
                tokio::task::yield_now().await;
            }
        })
        .await;
        let issued = operations(&mock, "tab.create").len();
        release.notify_one();
        assert_eq!(
            issued, 2,
            "a second tab creation issues before the first reply lands"
        );
        for id in [FIRST_SHELL, SECOND_SHELL] {
            wait_until("created shell viewport", || {
                websocket_requests(&mock, "terminal_set_viewport")
                    .iter()
                    .any(|request| request["terminal_id"] == id)
            })
            .await;
        }
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
    result.expect("two tab loop");
    for id in [FIRST_SHELL, SECOND_SHELL] {
        let pane = workspace.pane_for_terminal(id).expect("shell installed");
        assert_eq!(
            chrome
                .tabs()
                .tabs
                .iter()
                .filter(|tab| tab.slots.values().any(|p| *p == pane))
                .count(),
            1
        );
    }
    assert_ne!(
        mock.tab_for_terminal(FIRST_SHELL),
        mock.tab_for_terminal(SECOND_SHELL)
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_held_adoption_step_keeps_the_window_live_and_rolls_back_on_refusal() {
    let mock = MockDaemon::start("local-token").await;
    mock.refuse_workspace_op_at("pane.split", 1, "busy", "adoption split refused");
    // The focused pane types through the daemon, so both are tmux-backed (see
    // `pane_workspace`).
    mock.set_attach_backend("terminal-a", "tmux");
    mock.set_attach_backend("terminal-b", "tmux");
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        roster_items_on("tmux", &["terminal-a", "terminal-b"]),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("bare roster");
    let mut chrome = action_chrome();
    for id in workspace.roster_terminal_ids() {
        let pane = workspace.pane_for_terminal(&id).expect("bare terminal");
        chrome.open_pane(pane, workspace.pane(pane).display_name());
    }
    let original = chrome.active_tab().expect("bare tab").id.clone();
    assert!(chrome.active_tab().expect("bare tab").is_local());
    let focused = workspace
        .pane(chrome.focused_pane().expect("focus"))
        .terminal_id
        .clone();
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("terminal");
    let (input_tx, input_rx) = mpsc::channel(16);
    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches(&focused, 'x').await;
        let release = mock.hold_ws("workspace_op", |request| request["op"] == "tab.create");
        chord(&probe, 'i').await;
        wait_until("adoption create", || {
            operations(&mock, "tab.create").len() == 1
        })
        .await;
        assert!(
            operations(&mock, "pane.split").is_empty(),
            "chain waits for its predecessor"
        );
        assert_progress_while_held(&probe, &focused, &release).await;
        wait_until("adoption rollback", || {
            operations(&mock, "tab.close").len() == 1
        })
        .await;
        probe.key_reaches(&focused, 'w').await;
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
    result.expect("adoption refusal leaves loop running");
    assert_eq!(
        chrome.active_tab().expect("retained local tab").id,
        original
    );
    assert_eq!(
        chrome.active_tab().expect("retained local tab").slots.len(),
        2
    );
    let close = operations(&mock, "tab.close");
    assert_eq!(close.len(), 1, "rollback runs exactly once");
    assert!(workspace
        .workspace_model()
        .expect("model")
        .tab(close[0]["tab"].as_str().expect("captured tab"))
        .is_none());
    assert!(
        websocket_requests(&mock, "terminal_kill").is_empty(),
        "adopted terminals survive rollback"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn closing_a_tab_issues_tab_close_only_after_every_kill_settles() {
    for refuse_second in [false, true] {
        let mock = MockDaemon::start("local-token").await;
        let (mut workspace, _home) = pane_workspace(
            &mock,
            &["terminal-a", "terminal-b", "terminal-c"],
            None,
            false,
            &[],
        )
        .await;
        let seeded = mock.seed_workspace(
            "project-1",
            &[
                (&["terminal-a", "terminal-b"], "terminal-b"),
                (&["terminal-c"], "terminal-c"),
            ],
        );
        let closing_tab = seeded[0].0.clone();
        workspace
            .reconcile_subscribe_first()
            .await
            .expect("two tabs");
        let mut chrome = action_chrome();
        sync_live_chrome(&mut workspace, &mut chrome);
        assert!(chrome.focus_pane(
            workspace
                .pane_for_terminal("terminal-b")
                .expect("closing pane")
        ));
        let (backend, draws, frames) = DrawRecorder::new(96, 30);
        let mut terminal = Terminal::new(backend).expect("terminal");
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
            wait_for_replies(&mock, "terminal_take_control", 1).await;
            let first = mock.hold_ws("terminal_kill", |_| true);
            let second = mock.hold_ws("terminal_kill", |_| true);
            probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            probe.key(KeyCode::Char('X'), KeyModifiers::SHIFT).await;
            wait_for_websocket_requests(&mock, "terminal_kill", 1).await;
            assert!(
                operations(&mock, "tab.close").is_empty(),
                "first kill is outstanding"
            );
            chord(&probe, 'n').await;
            wait_until("surviving tab control", || {
                websocket_requests(&mock, "terminal_take_control")
                    .iter()
                    .any(|request| request["terminal_id"] == "terminal-c")
            })
            .await;
            assert_progress_while_held(&probe, "terminal-c", &first).await;
            wait_for_websocket_requests(&mock, "terminal_kill", 2).await;
            assert!(
                operations(&mock, "tab.close").is_empty(),
                "last kill is outstanding"
            );
            // The mock chooses a kill refusal when it receives the request,
            // so this case refuses the first kill; the second still settles.
            assert_progress_while_held(&probe, "terminal-c", &second).await;
            if refuse_second {
                wait_for_replies(&mock, "terminal_kill", 2).await;
                let mut probe_frames = LivenessProbe::new(
                    &mock,
                    &input_tx,
                    Arc::clone(&probe.draws),
                    Arc::clone(&probe.frames),
                );
                probe_frames.frames_render("terminal-c").await;
                probe.key_reaches("terminal-c", 'w').await;
            } else {
                wait_until("one tab close after both kills", || {
                    operations(&mock, "tab.close").len() == 1
                })
                .await;
            }
            drop(input_tx);
        };
        if refuse_second {
            mock.enqueue_kill_refusal("kill refused");
        }
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
        result.expect("close loop");
        assert_eq!(
            operations(&mock, "tab.close").len(),
            usize::from(!refuse_second)
        );
        assert_eq!(
            workspace
                .workspace_model()
                .expect("model")
                .tab(&closing_tab)
                .is_some(),
            refuse_second,
            "a refused kill retains its pane and tab"
        );
        assert_eq!(
            websocket_requests(&mock, "terminal_kill").len(),
            2,
            "each owned pane is killed once"
        );
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn a_held_arrange_or_grid_op_keeps_frames_flowing_and_places_from_returned_ids() {
    for grid in [false, true] {
        let mock = MockDaemon::start("local-token").await;
        mock.queue_owned_terminal_id(FIRST_SHELL);
        mock.queue_owned_terminal_id(SECOND_SHELL);
        let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
        workspace
            .reconcile_subscribe_first()
            .await
            .expect("workspace");
        let mut chrome = action_chrome();
        sync_live_chrome(&mut workspace, &mut chrome);
        if grid {
            chrome.mode = Mode::ProjectDialog;
            chrome.dialog = Some(Dialog::NewGrid { rows: 1, cols: 2 });
        } else {
            let target = ArrangeTarget {
                tab: chrome.active_tab().expect("tab").id.clone(),
                pane: None,
            };
            chrome.menu = Some(build_menu(
                &workspace,
                &chrome,
                ContextMenuKind::Submenu(Submenu::Arrange(target)),
                (0, 0),
            ));
            chrome.mode = Mode::ContextMenu;
        }
        let held_op = if grid { "tab.create" } else { "pane.move" };
        let release = mock.hold_ws("workspace_op", move |request| request["op"] == held_op);
        let (backend, draws, frames) = DrawRecorder::new(96, 30);
        let mut terminal = Terminal::new(backend).expect("terminal");
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
            wait_for_replies(&mock, "terminal_take_control", 1).await;
            probe.key(KeyCode::Enter, KeyModifiers::NONE).await;
            wait_until("held arrange/grid op", || {
                !operations(&mock, held_op).is_empty()
            })
            .await;
            assert_progress_while_held(&probe, "terminal-b", &release).await;
            if grid {
                wait_until("grid split", || operations(&mock, "pane.split").len() == 1).await;
                for id in [FIRST_SHELL, SECOND_SHELL] {
                    wait_until("grid shell viewport", || {
                        websocket_requests(&mock, "terminal_set_viewport")
                            .iter()
                            .any(|request| request["terminal_id"] == id)
                    })
                    .await;
                }
            } else {
                wait_until("arrange resize", || {
                    !operations(&mock, "pane.resize").is_empty()
                })
                .await;
                probe.key_reaches("terminal-b", 'w').await;
            }
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
        result.expect("arrange/grid loop");
        if grid {
            let model = workspace.workspace_model().expect("grid model");
            let first = model
                .panes()
                .find(|p| p.terminal_id.as_deref() == Some(FIRST_SHELL))
                .expect("first returned pane");
            assert_eq!(
                operations(&mock, "pane.split")[0]["pane"],
                first.id,
                "grid splits from the daemon-returned pane id"
            );
            assert_eq!(
                mock.tab_for_terminal(FIRST_SHELL),
                mock.tab_for_terminal(SECOND_SHELL)
            );
        }
        mock.shutdown().await;
    }
}
