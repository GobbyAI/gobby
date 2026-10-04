use gobby_client::app::{
    apply_live_menu_action, build_menu, project_dialog_key, ContextMenuKind, MenuAction,
    ModalOutcome,
};
use gobby_client::daemon::LiveDaemon;
use gobby_client::ui::dialogs::{render_dialog, Dialog};
use gobby_client::ui::menu_bar::MenuBarMenu;
use gobby_client::ui::Chrome;
use gobby_client::ui::Mode;
use gobby_client::Workspace;
use ratatui::backend::TestBackend;
use ratatui::crossterm::event::{KeyCode, KeyEvent};
use ratatui::layout::Rect;
use ratatui::Terminal;
use std::time::Duration;
mod mock_daemon;
use mock_daemon::MockDaemon;

#[test]
fn about_dialog_is_72_by_15_with_the_goblin_and_the_stated_rows() {
    let mut chrome = Chrome::dark();
    chrome.dialog = Some(Dialog::About {
        url: "http://127.0.0.1:60887".into(),
        gclient_version: "0.5.0".into(),
        daemon_version: Some("0.5.0".into()),
        machine: "workstation".into(),
    });
    let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("test terminal");
    terminal
        .draw(|frame| {
            render_dialog(frame, Rect::new(0, 0, 100, 30), &chrome);
        })
        .expect("render about dialog");
    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(14, 7)].symbol(), "┌");
    assert_eq!(buffer[(85, 21)].symbol(), "┘");
    assert!((15..85).all(|x| buffer[(x, 21)].symbol() == "─"));
    assert_eq!(buffer[(75, 8)].fg, chrome.palette.accent);
    assert_eq!(buffer[(47, 13)].fg, chrome.palette.subtext0);
    assert_eq!(buffer[(55, 13)].fg, chrome.palette.text);
    let row = |y| {
        (14..86)
            .map(|x| buffer[(x, y)].symbol())
            .collect::<String>()
    };
    assert!(row(8).contains("About gobby"));
    assert!(row(8).contains("esc Close"));
    let body: String = (9..21).map(row).collect();
    for expected in [
        "Gobby",
        "fleet management for AI coding agents",
        "gclient",
        "daemon",
        "url",
        "machine",
        "workstation",
        "gobby.ai",
    ] {
        assert!(body.contains(expected), "missing {expected} in {body}");
    }
    let accent_cells = (9..21)
        .flat_map(|y| (16..45).map(move |x| (x, y)))
        .filter(|&(x, y)| buffer[(x, y)].fg == chrome.palette.accent)
        .count();
    assert!(accent_cells > 10, "goblin mark is missing");
}

#[test]
fn daemon_dialog_shows_url_versions_health_and_last_roster_refresh() {
    let mut chrome = Chrome::dark();
    chrome.dialog = Some(Dialog::Daemon {
        url: "http://127.0.0.1:60887".into(),
        gclient_version: "0.5.0".into(),
        daemon_version: Some("0.5.0".into()),
        health: "ok".into(),
        last_roster_refresh: Some(Duration::from_secs(12)),
        stages: Some("ready in 842 ms".into()),
    });
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).expect("test terminal");
    terminal
        .draw(|frame| {
            render_dialog(frame, Rect::new(0, 0, 80, 24), &chrome);
        })
        .expect("render daemon dialog");
    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(12, 6)].symbol(), "┌");
    assert_eq!(buffer[(67, 16)].symbol(), "┘");
    let visible: String = (7..16)
        .flat_map(|y| (13..67).map(move |x| buffer[(x, y)].symbol()))
        .collect();
    for expected in [
        "Daemon",
        "esc Close",
        "url",
        "http://127.0.0.1:60887",
        "gclient",
        "0.5.0",
        "health",
        "ok",
        "roster",
        "refreshed 12 s ago",
        "startup",
        "ready in 842 ms",
    ] {
        assert!(
            visible.contains(expected),
            "missing {expected} in {visible}"
        );
    }
}

#[test]
fn info_dialogs_close_on_esc_enter_and_q() {
    let dialogs = [
        Dialog::Daemon {
            url: String::new(),
            gclient_version: String::new(),
            daemon_version: None,
            health: String::new(),
            last_roster_refresh: None,
            stages: None,
        },
        Dialog::About {
            url: String::new(),
            gclient_version: String::new(),
            daemon_version: None,
            machine: String::new(),
        },
    ];
    for dialog in dialogs {
        for close_key in [KeyCode::Esc, KeyCode::Enter, KeyCode::Char('q')] {
            let mut chrome = Chrome::dark();
            chrome.mode = Mode::ProjectDialog;
            chrome.dialog = Some(dialog.clone());
            assert_eq!(
                project_dialog_key(&mut chrome, &KeyEvent::from(KeyCode::Char('x'))),
                ModalOutcome::Consumed
            );
            assert!(chrome.dialog.is_some());
            assert_eq!(
                project_dialog_key(&mut chrome, &KeyEvent::from(close_key)),
                ModalOutcome::Close
            );
            assert!(chrome.dialog.is_none());
            assert_eq!(chrome.mode, Mode::Terminal);
        }
    }
}

#[tokio::test]
async fn help_menu_ends_with_about_gobby_and_both_entries_open() {
    let workspace = Workspace::scripted();
    let chrome = Chrome::dark();
    let help = build_menu(
        &workspace,
        &chrome,
        ContextMenuKind::MenuBar(MenuBarMenu::Help),
        (0, 0),
    );
    let labels: Vec<_> = help.items.iter().map(|item| item.label).collect();
    assert_eq!(labels, ["Keys", "Alerts…", "Daemon", "About Gobby"]);

    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect isolated daemon");
    let mut workspace = Workspace::live(daemon);
    let mut chrome = Chrome::dark();
    chrome.connection.url = "http://127.0.0.1:60887".into();
    chrome.connection.machine = "workstation".into();
    chrome.connection.daemon_version = Some("0.5.0".into());
    for (action, is_expected) in [
        (MenuAction::ShowDaemon, true),
        (MenuAction::ShowAbout, false),
    ] {
        apply_live_menu_action(
            &mut workspace,
            &mut chrome,
            ContextMenuKind::MenuBar(MenuBarMenu::Help),
            action,
        )
        .await
        .expect("open info dialog");
        assert_eq!(chrome.mode, Mode::ProjectDialog);
        assert_eq!(
            matches!(chrome.dialog, Some(Dialog::Daemon { .. })),
            is_expected
        );
        assert_eq!(
            matches!(chrome.dialog, Some(Dialog::About { .. })),
            !is_expected
        );
        match chrome.dialog.as_ref().expect("info dialog") {
            Dialog::Daemon {
                url,
                gclient_version,
                daemon_version,
                health,
                last_roster_refresh,
                ..
            } => {
                assert_eq!(url, "http://127.0.0.1:60887");
                assert_eq!(gclient_version, env!("CARGO_PKG_VERSION"));
                assert_eq!(daemon_version.as_deref(), Some("0.5.0"));
                assert_eq!(health, "unreachable: daemon unavailable");
                assert!(last_roster_refresh.is_none());
            }
            Dialog::About {
                url,
                gclient_version,
                daemon_version,
                machine,
            } => {
                assert_eq!(url, "http://127.0.0.1:60887");
                assert_eq!(gclient_version, env!("CARGO_PKG_VERSION"));
                assert_eq!(daemon_version.as_deref(), Some("0.5.0"));
                assert_eq!(
                    machine,
                    gobby_client::ui::sidebar::local_hostname().unwrap_or("workstation")
                );
            }
            other => panic!("unexpected dialog: {other:?}"),
        }
    }
}
