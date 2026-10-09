use gobby_client::app::{
    apply_live_menu_action, build_menu, project_dialog_key, ContextMenuKind, MenuAction,
    ModalOutcome,
};
use gobby_client::daemon::LiveDaemon;
use gobby_client::theme::{Theme, ThemeKind};
use gobby_client::ui::dialogs::{render_dialog, Dialog};
use gobby_client::ui::marks::{self, MarkPalette};
use gobby_client::ui::menu_bar::MenuBarMenu;
use gobby_client::ui::Chrome;
use gobby_client::ui::Mode;
use gobby_client::Workspace;
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::crossterm::event::{KeyCode, KeyEvent};
use ratatui::layout::Rect;
use ratatui::style::Color;
use ratatui::Terminal;
use std::time::Duration;
use tokio::sync::mpsc::unbounded_channel;
mod mock_daemon;
use mock_daemon::MockDaemon;

fn about_chrome(kind: ThemeKind) -> Chrome {
    let mut chrome = Chrome::new(Theme::new(kind));
    chrome.dialog = Some(Dialog::About {
        url: "http://127.0.0.1:60887".into(),
        gclient_version: "0.5.0".into(),
        daemon_version: Some("0.5.0".into()),
        machine: "workstation".into(),
    });
    chrome
}

fn draw_about(chrome: &Chrome, cols: u16, rows: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(cols, rows)).expect("test terminal");
    terminal
        .draw(|frame| {
            render_dialog(frame, Rect::new(0, 0, cols, rows), chrome);
        })
        .expect("render about dialog");
    terminal.backend().buffer().clone()
}

/// The approved v3 About: a 101x22 panel with the haloed goblin beside the
/// braille wordmark, one blank row under the wordmark and one above the
/// bottom border, in dark and light.
#[test]
fn about_dialog_is_101_by_22_with_the_goblin_beside_the_wordmark() {
    for (kind, wordmark_ink) in [
        (ThemeKind::Dark, None),
        (ThemeKind::Light, Some(Color::Rgb(0x15, 0x17, 0x14))),
    ] {
        let chrome = about_chrome(kind);
        let p = &chrome.palette;
        let buffer = draw_about(&chrome, 120, 30);
        // The panel spans columns 9..=109 and rows 4..=25; inside it the
        // header is row 5, the goblin rows 6..=23, the wordmark rows 7..=14.
        assert_eq!(buffer[(9, 4)].symbol(), "┌", "{kind:?}");
        assert_eq!(buffer[(109, 25)].symbol(), "┘", "{kind:?}");
        assert!((10..109).all(|x| buffer[(x, 25)].symbol() == "─"));
        let row = |y: u16| {
            (10..109)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        };
        assert!(row(5).contains("About gobby"));
        assert!(row(5).contains("esc Close"));

        let goblin = marks::goblin_large();
        let mut alone = Terminal::new(TestBackend::new(goblin.cols, goblin.rows)).unwrap();
        alone
            .draw(|frame| {
                marks::render_mark(frame, (0, 0), goblin, &MarkPalette::normal(p, false));
            })
            .unwrap();
        let alone = alone.backend().buffer();
        for (gx, gy) in (0..goblin.rows).flat_map(|y| (0..goblin.cols).map(move |x| (x, y))) {
            let (want, got) = (&alone[(gx, gy)], &buffer[(11 + gx, 6 + gy)]);
            assert_eq!(
                got.symbol(),
                want.symbol(),
                "{kind:?} goblin cell {gx},{gy}"
            );
            if want.symbol() != " " {
                assert_eq!(got.fg, want.fg, "{kind:?} goblin cell {gx},{gy}");
            }
        }

        let braille: Vec<_> = (7..15)
            .flat_map(|y| (54..108).map(move |x| (x, y)))
            .filter(|&(x, y)| {
                ('\u{2801}'..='\u{28FF}').contains(&buffer[(x, y)].symbol().chars().next().unwrap())
            })
            .collect();
        assert!(braille.len() > 100, "{kind:?} wordmark is missing");
        let ink = wordmark_ink.unwrap_or(p.accent);
        assert!(
            braille.iter().all(|&(x, y)| buffer[(x, y)].fg == ink),
            "{kind:?}"
        );

        let blank =
            |y: u16, xs: std::ops::Range<u16>| xs.clone().all(|x| buffer[(x, y)].symbol() == " ");
        assert!(blank(15, 54..108), "{kind:?} blank row under the wordmark");
        assert!(
            blank(24, 10..109),
            "{kind:?} blank row above the bottom border"
        );

        let text = |y: u16| {
            (54..108)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        };
        for (y, expected) in [
            (16, "Fleet management for AI agents"),
            (18, "gclient 0.5.0"),
            (19, "daemon  0.5.0"),
            (20, "url     http://127.0.0.1:60887"),
            (21, "machine workstation"),
            (23, "gobby.ai"),
        ] {
            assert!(
                text(y).starts_with(expected),
                "{kind:?} row {y}: {}",
                text(y)
            );
        }
        assert_eq!(buffer[(54, 16)].fg, p.subtext0);
        assert_eq!(buffer[(54, 18)].fg, p.subtext0);
        assert_eq!(buffer[(62, 18)].fg, p.text);
        assert_eq!(buffer[(54, 23)].fg, p.overlay0);
    }
}

/// A terminal too small for the whole panel drops the goblin first, then the
/// wordmark, and keeps the text.
#[test]
fn a_small_terminal_drops_the_goblin_then_the_wordmark() {
    let chrome = about_chrome(ThemeKind::Dark);
    let has_braille = |buffer: &Buffer| {
        buffer
            .content()
            .iter()
            .any(|cell| ('\u{2801}'..='\u{28FF}').contains(&cell.symbol().chars().next().unwrap()))
    };
    let has_halfblock = |buffer: &Buffer| {
        buffer
            .content()
            .iter()
            .any(|cell| matches!(cell.symbol(), "▀" | "▄"))
    };
    let text = |buffer: &Buffer| {
        buffer
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>()
    };

    let wide = draw_about(&chrome, 80, 30);
    assert!(
        !has_halfblock(&wide) && has_braille(&wide),
        "80 columns: the wordmark alone"
    );
    let narrow = draw_about(&chrome, 50, 24);
    assert!(
        !has_halfblock(&narrow) && !has_braille(&narrow),
        "50 columns: text only"
    );
    for buffer in [&wide, &narrow] {
        for expected in [
            "About gobby",
            "Fleet management for AI agents",
            "workstation",
            "gobby.ai",
        ] {
            assert!(text(buffer).contains(expected), "missing {expected}");
        }
    }
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
    assert_eq!(labels, ["Keybinds", "Alerts…", "Daemon", "About Gobby"]);

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
            &unbounded_channel().0,
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
