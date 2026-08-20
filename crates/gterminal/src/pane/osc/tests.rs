use tokio::sync::mpsc;

use super::*;
use crate::layout::PaneId;

fn pane_default_theme(
    pane: &super::super::GhosttyPaneTerminal,
) -> crate::terminal_theme::TerminalTheme {
    let mut core = pane.core.lock().unwrap();
    let super::super::terminal::GhosttyPaneCore {
        terminal,
        render_state,
        ..
    } = &mut *core;
    render_state.update(terminal).unwrap();
    let colors = render_state.colors().unwrap();
    crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: colors.foreground.r,
            g: colors.foreground.g,
            b: colors.foreground.b,
        }),
        background: Some(crate::terminal_theme::RgbColor {
            r: colors.background.r,
            g: colors.background.g,
            b: colors.background.b,
        }),
        ..Default::default()
    }
}

fn shell_job(shell_pid: u32) -> crate::platform::ForegroundJob {
    crate::platform::ForegroundJob {
        process_group_id: shell_pid,
        processes: vec![crate::platform::ForegroundProcess {
            pid: shell_pid,
            name: "zsh".to_string(),
            argv0: Some("zsh".to_string()),
            argv: Some(vec!["zsh".to_string()]),
            cmdline: Some("zsh".to_string()),
        }],
    }
}

fn tracked_default_color_events(events: Vec<DefaultColorTrackedEvent>) -> Vec<DefaultColorEvent> {
    events.into_iter().map(|event| event.event).collect()
}

fn enabled_osc_debug_tracker() -> OscDebugTracker {
    OscDebugTracker {
        enabled: true,
        collector: OscStreamCollector::default(),
        pending: Vec::new(),
    }
}

#[test]
fn osc_stream_collector_ignores_strings_and_preserves_escaped_bytes() {
    let mut collector = OscStreamCollector::default();
    let mut bodies = Vec::new();

    collector.observe(
        b"\x1bPignored\x1b]0;not-osc\x07\x1b\\\x1b]9;a\x1b",
        |body| bodies.push(body.to_vec()),
    );
    collector.observe(b"\x1b\\\x1b]2;b\x1b\x07", |body| bodies.push(body.to_vec()));

    assert_eq!(bodies, vec![b"9;a\x1b".to_vec(), b"2;b\x1b".to_vec()]);
}

#[test]
fn default_color_tracker_detects_split_osc_11_sequences() {
    let mut tracker = DefaultColorOscTracker::default();

    assert!(!tracker.observe(b"\x1b]11;rgb:11/22"));
    assert!(tracker.observe(b"/33\x1b\\"));
}

#[test]
fn default_color_tracker_ignores_osc_queries() {
    let mut tracker = DefaultColorOscTracker::default();

    assert!(!tracker.observe(b"\x1b]10;?\x1b\\"));
    assert!(!tracker.observe(b"\x1b]11;?\x07"));
}

#[test]
fn reported_cwd_parses_file_uri_and_bare_paths() {
    assert_eq!(
        parse_reported_cwd(b"file:///tmp/gterm%20repo"),
        Some(std::path::PathBuf::from("/tmp/gterm repo"))
    );
    assert_eq!(
        parse_reported_cwd(b"C:\\Users\\gterm\\src\\gterm"),
        Some(std::path::PathBuf::from("C:\\Users\\gterm\\src\\gterm"))
    );
    assert_eq!(
        parse_reported_cwd(b"\"C:\\my proj\""),
        Some(std::path::PathBuf::from("C:\\my proj"))
    );
}

#[test]
fn reported_cwd_rejects_invalid_or_empty_values() {
    assert_eq!(parse_reported_cwd(b""), None);
    assert_eq!(parse_reported_cwd(b"\xff"), None);
    assert_eq!(parse_reported_cwd(b"file://remote/tmp"), None);
}

// -----------------------------------------------------------------------
// AgentOscStateTracker tests
// -----------------------------------------------------------------------

#[test]
fn agent_osc_osc0_title_with_bel() {
    let mut t = AgentOscStateTracker::default();
    t.observe("hello\x1b]0;braille title\x07world".as_bytes());
    assert_eq!(t.latest_title(), "braille title");
    assert_eq!(t.terminal_title(), Some("braille title"));
    assert_eq!(t.latest_progress(), "");
}

#[test]
fn agent_osc_osc2_title_with_st() {
    let mut t = AgentOscStateTracker::default();
    t.observe("hello\x1b]2;static title\x1b\\world".as_bytes());
    assert_eq!(t.latest_title(), "static title");
    assert_eq!(t.latest_progress(), "");
}

#[test]
fn agent_osc_empty_osc0_clears_title() {
    let mut t = AgentOscStateTracker::default();
    // First set a title.
    t.observe(b"\x1b]0;some title\x07");
    assert_eq!(t.latest_title(), "some title");
    // Then clear it with an empty payload (Codex pattern).
    t.observe(b"\x1b]0;\x07");
    assert_eq!(t.latest_title(), "");
    assert_eq!(t.terminal_title(), None);
}

#[test]
fn clearing_agent_evidence_preserves_the_terminal_title() {
    let mut tracker = AgentOscStateTracker::default();
    tracker.observe("\x1b]2;✳ 修复🙂标题\x1b\\".as_bytes());

    tracker.clear_retained();

    assert_eq!(tracker.latest_title(), "");
    assert_eq!(tracker.terminal_title(), Some("✳ 修复🙂标题"));
}

#[cfg(unix)]
#[test]
fn handoff_seed_does_not_restore_osc_evidence() {
    let mut tracker = AgentOscStateTracker::default();

    tracker.seed_terminal_title(Some("✳ restored title".into()));

    assert_eq!(tracker.terminal_title(), Some("✳ restored title"));
    assert_eq!(tracker.latest_title(), "");
}

#[test]
fn agent_osc_osc9_sets_progress_with_bel() {
    let mut t = AgentOscStateTracker::default();
    t.observe(b"\x1b]9;4;3;\x07");
    assert_eq!(t.latest_progress(), "4;3;");
    assert_eq!(t.latest_title(), "");
}

#[test]
fn agent_osc_osc9_clear_progress_with_st() {
    let mut t = AgentOscStateTracker::default();
    t.observe(b"\x1b]9;4;3;\x07");
    assert_eq!(t.latest_progress(), "4;3;");
    t.observe(b"\x1b]9;4;0;\x1b\\");
    assert_eq!(t.latest_progress(), "4;0;");
}

#[test]
fn agent_osc_split_sequence_across_chunks() {
    let mut t = AgentOscStateTracker::default();
    t.observe(b"\x1b]9;4;3");
    assert_eq!(t.latest_progress(), "");
    t.observe(b";\x07");
    assert_eq!(t.latest_progress(), "4;3;");
}

#[test]
fn agent_osc_bel_and_st_terminators_both_work() {
    let mut t = AgentOscStateTracker::default();
    t.observe(b"\x1b]0;title-bel\x07");
    assert_eq!(t.latest_title(), "title-bel");
    t.observe(b"\x1b]0;title-st\x1b\\");
    assert_eq!(t.latest_title(), "title-st");
}

#[test]
fn agent_osc_oversized_payload_is_discarded_and_recovers() {
    let mut t = AgentOscStateTracker::default();
    // Set a title first.
    t.observe(b"\x1b]0;before\x07");
    assert_eq!(t.latest_title(), "before");

    // Feed an oversized OSC body (> 4096 bytes).
    let mut oversized = Vec::from(b"\x1b]0;".as_slice());
    oversized.extend(std::iter::repeat_n(b'x', 4097));
    oversized.push(0x07);
    t.observe(&oversized);
    // The oversized body is dropped; the previously stored title is kept.
    assert_eq!(t.latest_title(), "before");

    // After recovery, subsequent valid sequences are captured normally.
    t.observe(b"\x1b]0;after\x07");
    assert_eq!(t.latest_title(), "after");
}

#[test]
fn agent_osc_cap_length_is_respected() {
    let mut t = AgentOscStateTracker::default();
    // Build a title of AGENT_OSC_MAX_CHARS + 50 ASCII chars.
    let long_title: String = "a".repeat(AGENT_OSC_MAX_CHARS + 50);
    let seq = format!("\x1b]0;{long_title}\x07");
    t.observe(seq.as_bytes());
    assert_eq!(t.latest_title().len(), AGENT_OSC_MAX_CHARS);
}

#[test]
fn agent_osc_control_chars_stripped() {
    let mut t = AgentOscStateTracker::default();
    t.observe(b"\x1b]0;before\x01after\x07");
    assert_eq!(t.latest_title(), "beforeafter");
}

#[test]
fn agent_osc_unrelated_osc_does_not_overwrite_title() {
    let mut t = AgentOscStateTracker::default();
    t.observe(b"\x1b]0;my title\x07");
    // OSC 4 (palette color), OSC 52 (clipboard) — should not touch title/progress.
    t.observe(b"\x1b]4;1;rgb:aa/bb/cc\x07");
    t.observe(b"\x1b]52;c;aGVsbG8=\x07");
    assert_eq!(t.latest_title(), "my title");
    assert_eq!(t.latest_progress(), "");
}

#[test]
fn agent_osc_interleaved_sequences() {
    let mut t = AgentOscStateTracker::default();
    // OSC 0 title, then OSC 9 progress, then OSC 2 title update.
    t.observe(b"\x1b]0;first\x07\x1b]9;4;3;\x07\x1b]2;second\x07");
    assert_eq!(t.latest_title(), "second");
    assert_eq!(t.latest_progress(), "4;3;");
}

#[test]
fn agent_osc_default_state_is_empty() {
    let t = AgentOscStateTracker::default();
    assert_eq!(t.latest_title(), "");
    assert_eq!(t.latest_progress(), "");
}

// -----------------------------------------------------------------------
// OscDebugTracker tests (existing)
// -----------------------------------------------------------------------

#[test]
fn osc_debug_tracker_detects_title_with_bel() {
    let mut tracker = enabled_osc_debug_tracker();

    tracker.observe("hello\x1b]0;✻ working title\x07world".as_bytes());

    assert_eq!(
        tracker.drain_pending(),
        vec![OscDebugEvent {
            command: "0".to_string(),
            payload: "✻ working title".to_string(),
        }]
    );
}

#[test]
fn osc_debug_tracker_detects_title_with_st() {
    let mut tracker = enabled_osc_debug_tracker();

    tracker.observe("hello\x1b]2;static title\x1b\\world".as_bytes());

    assert_eq!(
        tracker.drain_pending(),
        vec![OscDebugEvent {
            command: "2".to_string(),
            payload: "static title".to_string(),
        }]
    );
}

#[test]
fn osc_debug_tracker_detects_split_status_sequences() {
    let mut tracker = enabled_osc_debug_tracker();

    tracker.observe(b"\x1b]9;4;3");
    assert!(tracker.drain_pending().is_empty());
    tracker.observe(b"\x07\x1b]21337;status=working\x1b\\");

    assert_eq!(
        tracker.drain_pending(),
        vec![
            OscDebugEvent {
                command: "9".to_string(),
                payload: "4;3".to_string(),
            },
            OscDebugEvent {
                command: "21337".to_string(),
                payload: "status=working".to_string(),
            },
        ]
    );
}

#[test]
fn osc_debug_tracker_ignores_untracked_osc_commands() {
    let mut tracker = enabled_osc_debug_tracker();

    tracker.observe(b"\x1b]52;c;SGVsbG8=\x07\x1b]7;file:///tmp\x07");

    assert!(tracker.drain_pending().is_empty());
}

#[test]
fn osc_debug_tracker_sanitizes_control_characters() {
    let mut tracker = enabled_osc_debug_tracker();

    tracker.observe(b"\x1b]0;before\x01after\x07");

    assert_eq!(
        tracker.drain_pending(),
        vec![OscDebugEvent {
            command: "0".to_string(),
            payload: "beforeafter".to_string(),
        }]
    );
}

#[test]
fn osc_debug_tracker_recovers_after_oversized_payload() {
    let mut tracker = enabled_osc_debug_tracker();
    let oversized = vec![b'a'; 4097];

    tracker.observe(b"\x1b]0;");
    tracker.observe(&oversized);
    tracker.observe(b"\x07\x1b]0;ok\x07");

    assert_eq!(
        tracker.drain_pending(),
        vec![OscDebugEvent {
            command: "0".to_string(),
            payload: "ok".to_string(),
        }]
    );
}

#[test]
fn default_color_event_tracker_detects_queries_sets_and_resets() {
    let mut tracker = DefaultColorEventTracker::default();

    tracker.observe(
            b"\x1b]10;?\x07\x1b]11;?\x1b\\\x1b]12;?\x07\x1b]4;0;?\x07\x1b]10;rgb:11/22/33\x07\x1b]111\x07",
        );

    assert_eq!(
        tracked_default_color_events(tracker.drain_pending()),
        vec![
            DefaultColorEvent::Query(DefaultColorQuery::Foreground),
            DefaultColorEvent::Query(DefaultColorQuery::Background),
            DefaultColorEvent::Query(DefaultColorQuery::Cursor),
            DefaultColorEvent::PaletteQuery(0),
            DefaultColorEvent::Set(DefaultColorQuery::Foreground),
            DefaultColorEvent::Reset(DefaultColorQuery::Background),
        ]
    );
}

#[test]
fn default_color_event_tracker_tracks_each_multi_value_set() {
    let mut tracker = DefaultColorEventTracker::default();

    tracker.observe(
            b"\x1b]10;rgb:11/22/33;rgb:44/55/66\x1b\\\x1b]10;?;rgb:77/88/99\x1b\\\x1b]10;;rgb:aa/bb/cc\x1b\\",
        );

    assert_eq!(
        tracked_default_color_events(tracker.drain_pending()),
        vec![
            DefaultColorEvent::Set(DefaultColorQuery::Foreground),
            DefaultColorEvent::Set(DefaultColorQuery::Background),
            DefaultColorEvent::Set(DefaultColorQuery::Background),
            DefaultColorEvent::Set(DefaultColorQuery::Foreground),
        ]
    );
}

#[test]
fn default_color_event_tracker_handles_split_default_color_queries() {
    let mut tracker = DefaultColorEventTracker::default();

    tracker.observe(b"\x1b]11");
    assert!(tracker.drain_pending().is_empty());
    tracker.observe(b";?\x1b");
    assert!(tracker.drain_pending().is_empty());
    tracker.observe(b"\\");

    assert_eq!(
        tracked_default_color_events(tracker.drain_pending()),
        vec![DefaultColorEvent::Query(DefaultColorQuery::Background)]
    );
}

#[test]
fn default_color_event_tracker_handles_split_palette_color_queries() {
    let mut tracker = DefaultColorEventTracker::default();

    tracker.observe(b"\x1b]4;25");
    assert!(tracker.drain_pending().is_empty());
    tracker.observe(b"5;?\x1b");
    assert!(tracker.drain_pending().is_empty());
    tracker.observe(b"\\");

    assert_eq!(
        tracked_default_color_events(tracker.drain_pending()),
        vec![DefaultColorEvent::PaletteQuery(255)]
    );
}

#[test]
fn default_color_event_tracker_rejects_malformed_palette_color_queries() {
    let mut tracker = DefaultColorEventTracker::default();

    tracker.observe(b"\x1b]4;;?\x07");
    tracker.observe(b"\x1b]4;-1;?\x07");
    tracker.observe(b"\x1b]4;256;?\x07");
    tracker.observe(b"\x1b]4;0;?;1;?\x07");
    tracker.observe(b"\x1b]4;0;rgb:1111/2222/3333\x07");
    tracker.observe(b"\x1b]4;0;?\x07");

    assert_eq!(
        tracked_default_color_events(tracker.drain_pending()),
        vec![DefaultColorEvent::PaletteQuery(0)]
    );
}

#[test]
fn default_color_event_tracker_ignores_other_osc_and_dcs_payloads() {
    let mut tracker = DefaultColorEventTracker::default();

    tracker.observe(b"\x1b]0;title\x07");
    tracker.observe(b"\x1b]52;c;?\x07");
    tracker.observe(b"\x1bPtmux;\x1b\x1b]11;?\x07\x1b\\");
    tracker.observe(b"\x1bPtmux;payload\x07\x1b]11;?\x07\x1b\\");

    assert!(tracker.drain_pending().is_empty());
}

#[test]
fn default_color_event_tracker_ignores_oversized_osc_until_terminator() {
    let mut tracker = DefaultColorEventTracker::default();
    let mut oversized = Vec::from(b"\x1b]11;".as_slice());
    oversized.extend(std::iter::repeat_n(b'a', 1025));
    oversized.extend_from_slice(b"\x1b]11;?\x07");

    tracker.observe(&oversized);
    assert!(tracker.drain_pending().is_empty());

    tracker.observe(b"\x1b]11;?\x07");
    assert_eq!(
        tracked_default_color_events(tracker.drain_pending()),
        vec![DefaultColorEvent::Query(DefaultColorQuery::Background)]
    );
}

#[test]
fn droid_scrollback_compat_matches_process_name_and_cmdline() {
    let name_only = crate::platform::ForegroundJob {
        process_group_id: 42,
        processes: vec![crate::platform::ForegroundProcess {
            pid: 42,
            name: "droid".to_string(),
            argv0: None,
            argv: Some(vec![
                "/opt/factory/droid".to_string(),
                "--resume".to_string(),
            ]),
            cmdline: Some("/opt/factory/droid --resume".to_string()),
        }],
    };
    assert!(foreground_job_uses_droid_scrollback_compat(&name_only));

    let cmdline_only = crate::platform::ForegroundJob {
        process_group_id: 42,
        processes: vec![crate::platform::ForegroundProcess {
            pid: 42,
            name: "bun".to_string(),
            argv0: Some("bun".to_string()),
            argv: Some(vec![
                "bun".to_string(),
                "/home/can/.local/bin/droid".to_string(),
                "--resume".to_string(),
            ]),
            cmdline: Some("/home/can/.local/bin/droid --resume".to_string()),
        }],
    };
    assert!(foreground_job_uses_droid_scrollback_compat(&cmdline_only));

    let shell = shell_job(7);
    assert!(!foreground_job_uses_droid_scrollback_compat(&shell));
}

#[test]
fn strip_scrollback_clear_sequences_removes_ed3_only() {
    let filtered = strip_scrollback_clear_sequences(b"a\x1b[3Jb\x1b[?3Jc\x1b[2Jd");
    assert_eq!(filtered.as_ref(), b"abc\x1b[2Jd");
}

#[test]
fn primary_screen_droid_compat_ignores_scrollback_clear_only_for_droid() {
    let droid_job = crate::platform::ForegroundJob {
        process_group_id: 42,
        processes: vec![crate::platform::ForegroundProcess {
            pid: 42,
            name: "droid".to_string(),
            argv0: Some("droid".to_string()),
            argv: Some(vec!["droid".to_string()]),
            cmdline: Some("droid".to_string()),
        }],
    };

    let filtered =
        maybe_filter_primary_screen_scrollback_clear(b"\x1b[3J\x1b[2J", false, Some(&droid_job));
    assert_eq!(filtered.as_ref(), b"\x1b[2J");

    let shell =
        maybe_filter_primary_screen_scrollback_clear(b"\x1b[3J\x1b[2J", false, Some(&shell_job(7)));
    assert_eq!(shell.as_ref(), b"\x1b[3J\x1b[2J");

    let alternate =
        maybe_filter_primary_screen_scrollback_clear(b"\x1b[3J\x1b[2J", true, Some(&droid_job));
    assert_eq!(alternate.as_ref(), b"\x1b[3J\x1b[2J");
}

#[test]
fn host_theme_restore_waits_for_shell_and_non_alternate_screen() {
    assert!(!should_restore_host_terminal_theme(
        42,
        7,
        true,
        Some(&shell_job(7)),
    ));
    assert!(!should_restore_host_terminal_theme(42, 7, false, None));
    assert!(!should_restore_host_terminal_theme(
        42,
        7,
        false,
        Some(&crate::platform::ForegroundJob {
            process_group_id: 42,
            processes: vec![crate::platform::ForegroundProcess {
                pid: 42,
                name: "droid".to_string(),
                argv0: Some("droid".to_string()),
                argv: Some(vec!["droid".to_string()]),
                cmdline: Some("droid".to_string()),
            }],
        }),
    ));
    assert!(should_restore_host_terminal_theme(
        42,
        7,
        false,
        Some(&shell_job(7)),
    ));

    #[cfg(target_os = "macos")]
    assert!(should_restore_host_terminal_theme(
        7,
        7,
        false,
        Some(&shell_job(7)),
    ));

    #[cfg(not(target_os = "macos"))]
    assert!(!should_restore_host_terminal_theme(
        7,
        7,
        false,
        Some(&shell_job(7)),
    ));
}

#[test]
fn restore_host_terminal_theme_reapplies_cached_colors() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = super::super::GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let pane_id = PaneId::from_raw(1);
    let shell_pid = 7;
    let host_theme = crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0xaa,
            g: 0xbb,
            b: 0xcc,
        }),
        background: Some(crate::terminal_theme::RgbColor {
            r: 0x11,
            g: 0x22,
            b: 0x33,
        }),
        ..Default::default()
    };

    pane.apply_host_terminal_theme(host_theme);
    {
        let mut core = pane.core.lock().unwrap();
        core.transient_default_color_owner_pgid = Some(42);
        core.terminal.write(b"\x1b]11;rgb:dd/ee/ff\x1b\\");
    }
    assert_eq!(
        pane_default_theme(&pane).background,
        Some(crate::terminal_theme::RgbColor {
            r: 0xdd,
            g: 0xee,
            b: 0xff,
        })
    );

    {
        let mut core = pane.core.lock().unwrap();
        assert!(restore_host_terminal_theme_if_needed(
            &mut core,
            pane_id,
            shell_pid,
            false,
            Some(&shell_job(shell_pid)),
        ));
    }

    assert_eq!(pane_default_theme(&pane).background, host_theme.background);
    assert_eq!(pane_default_theme(&pane).foreground, host_theme.foreground);
}
