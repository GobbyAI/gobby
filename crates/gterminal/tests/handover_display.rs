//! Pane display state survives a host handover: the ghostty snapshot restores
//! the C terminal, and `PaneCoreHandover` restores the Rust wrapper state
//! around it.

use bytes::Bytes;
use gobby_terminal::ghostty::{ActiveScreen, RenderColors, RenderState, RgbColor, Terminal};
use gobby_terminal::layout::PaneId;
use gobby_terminal::pane::{GhosttyPaneTerminal, PaneCoreHandover, PANE_CONTINUATION_MAX_BYTES};
use gobby_terminal::terminal_theme::{self, HostAppearance, TerminalTheme};
use tokio::sync::mpsc;

const COLS: u16 = 20;
const ROWS: u16 = 6;
const SCROLLBACK_BYTES: usize = 1 << 20;
const MODE_ORIGIN: u16 = 6;
const MODE_WRAPAROUND: u16 = 7;
const MODE_CURSOR_VISIBLE: u16 = 25;
const MODE_ALT_SCREEN: u16 = 1049;
const PROBED_MODES: [u16; 7] = [
    gobby_terminal::ghostty::MODE_APPLICATION_CURSOR_KEYS,
    MODE_ORIGIN,
    MODE_WRAPAROUND,
    MODE_CURSOR_VISIBLE,
    MODE_ALT_SCREEN,
    gobby_terminal::ghostty::MODE_BRACKETED_PASTE,
    gobby_terminal::ghostty::MODE_GRAPHEME_CLUSTER,
];

fn tracked_terminal() -> Terminal {
    let mut terminal = Terminal::new(COLS, ROWS, SCROLLBACK_BYTES).expect("terminal");
    terminal
        .set_continuation_max_bytes(PANE_CONTINUATION_MAX_BYTES)
        .expect("enable continuation tracking");
    terminal
}

fn round_trip(terminal: &Terminal) -> Terminal {
    let snapshot = terminal.encode_snapshot().expect("encode snapshot");
    Terminal::decode_snapshot(&snapshot, PANE_CONTINUATION_MAX_BYTES).expect("decode snapshot")
}

fn last_screen_row(terminal: &Terminal) -> u32 {
    let rows = terminal.total_rows().expect("total rows");
    u32::try_from(rows.saturating_sub(1)).expect("row index fits u32")
}

/// Every row of the active screen, history included, as plain text.
fn screen_text(terminal: &Terminal) -> String {
    terminal
        .read_text_screen((0, 0), (COLS - 1, last_screen_row(terminal)), false)
        .expect("screen text")
}

/// Every row of the active screen, history included, with styles.
fn screen_ansi(terminal: &Terminal) -> String {
    terminal
        .read_ansi_screen((0, 0), (COLS - 1, last_screen_row(terminal)), false, false)
        .expect("screen ansi")
}

fn render_colors(terminal: &Terminal) -> RenderColors {
    let mut render_state = RenderState::new().expect("render state");
    render_state.update(terminal).expect("render update");
    render_state.colors().expect("render colors")
}

fn cursor(terminal: &Terminal) -> (Option<(u16, u16)>, bool) {
    let mut render_state = RenderState::new().expect("render state");
    render_state.update(terminal).expect("render update");
    let position = render_state
        .cursor_viewport()
        .expect("cursor viewport")
        .map(|cursor| (cursor.x, cursor.y));
    (
        position,
        render_state.cursor_visible().expect("cursor visible"),
    )
}

fn modes(terminal: &Terminal) -> Vec<(u16, bool)> {
    PROBED_MODES
        .iter()
        .map(|&mode| (mode, terminal.mode_get(mode).expect("mode")))
        .collect()
}

fn viewport_hyperlinks(terminal: &Terminal) -> Vec<(u16, u32, String)> {
    let mut links = Vec::new();
    for y in 0..u32::from(ROWS) {
        for x in 0..COLS {
            if let Some(uri) = terminal.viewport_hyperlink_uri(x, y).expect("hyperlink") {
                links.push((x, y, uri));
            }
        }
    }
    links
}

fn assert_same_display(original: &Terminal, restored: &Terminal) {
    assert_eq!(
        screen_text(restored),
        screen_text(original),
        "screen and history text"
    );
    assert_eq!(
        screen_ansi(restored),
        screen_ansi(original),
        "styled screen and history"
    );
    assert_eq!(cursor(restored), cursor(original), "cursor");
    assert_eq!(modes(restored), modes(original), "modes");
    assert_eq!(
        restored.active_screen().expect("active screen"),
        original.active_screen().expect("active screen"),
    );
    assert_eq!(
        restored.scrollback_rows().expect("scrollback rows"),
        original.scrollback_rows().expect("scrollback rows"),
    );
    assert_eq!(render_colors(restored), render_colors(original), "colors");
}

fn write_both(original: &mut Terminal, restored: &mut Terminal, bytes: &[u8]) {
    original.write(bytes);
    restored.write(bytes);
}

#[test]
fn snapshot_restores_screen_history_cursor_and_modes() {
    let mut original = tracked_terminal();
    for line in 0..30 {
        original.write(format!("history line {line}\r\n").as_bytes());
    }
    original.write(b"\x1b[1;31mred\x1b[0m \x1b]8;;https://example.com/x\x07link\x1b]8;;\x07\r\n");
    original.write(b"\x1b]4;1;rgb:12/34/56\x07");
    original.write(b"\x1b[?2004h");
    // Custom tab stops at columns 5 and 11 only.
    original.write(b"\x1b[3g\x1b[6G\x1bH\x1b[12G\x1bH");
    original.write(b"\x1b[2;4r");
    original.write(b"\x1b[3;7H");

    let mut restored = round_trip(&original);

    assert_same_display(&original, &restored);
    assert!(screen_text(&restored).contains("history line 0"));
    assert_eq!(
        render_colors(&restored).palette[1],
        RgbColor {
            r: 0x12,
            g: 0x34,
            b: 0x56
        },
    );
    assert!(restored
        .mode_get(gobby_terminal::ghostty::MODE_BRACKETED_PASTE)
        .expect("bracketed paste"));
    let links = viewport_hyperlinks(&restored);
    assert_eq!(links, viewport_hyperlinks(&original));
    assert!(links
        .iter()
        .any(|(_, _, uri)| uri == "https://example.com/x"));

    // Tab stops and the scrolling region act the same on later input.
    write_both(&mut original, &mut restored, b"\r\x1b[2K\tA\tB");
    assert_same_display(&original, &restored);
    assert!(screen_text(&restored).contains("     A     B"));
    write_both(&mut original, &mut restored, b"\x1b[4;1H\n\nZ");
    assert_same_display(&original, &restored);
}

#[test]
fn continuation_completes_split_sequences() {
    let mut original = tracked_terminal();
    original.write(b"plain \x1b[1;3");
    let mut restored = round_trip(&original);
    write_both(&mut original, &mut restored, b"1mbold red\x1b[0m");
    assert_same_display(&original, &restored);
    assert!(screen_text(&restored).contains("plain bold red"));
    assert!(!screen_text(&restored).contains("1mbold"));

    let mut original = tracked_terminal();
    original.write(b"euro \xe2\x82");
    let mut restored = round_trip(&original);
    write_both(&mut original, &mut restored, b"\xac!");
    assert_same_display(&original, &restored);
    assert!(screen_text(&restored).contains("euro \u{20ac}!"));
}

#[test]
fn alternate_screen_keeps_primary_history() {
    let mut original = tracked_terminal();
    for line in 0..30 {
        original.write(format!("primary line {line}\r\n").as_bytes());
    }
    original.write(b"\x1b[?1049h\x1b[Halternate view");

    let mut restored = round_trip(&original);

    assert_eq!(
        restored.active_screen().expect("active screen"),
        ActiveScreen::Alternate
    );
    assert_same_display(&original, &restored);
    assert!(screen_text(&restored).contains("alternate view"));

    write_both(&mut original, &mut restored, b"\x1b[?1049l");
    assert_eq!(
        restored.active_screen().expect("active screen"),
        ActiveScreen::Primary
    );
    assert_same_display(&original, &restored);
    assert!(screen_text(&restored).contains("primary line 0"));
    assert!(screen_text(&restored).contains("primary line 29"));
}

fn pane_terminal() -> (GhosttyPaneTerminal, mpsc::Sender<Bytes>) {
    let (sender, _receiver) = mpsc::channel(4);
    let terminal = Terminal::new(COLS, ROWS, SCROLLBACK_BYTES).expect("terminal");
    let pane = GhosttyPaneTerminal::new(terminal, sender.clone()).expect("pane terminal");
    (pane, sender)
}

fn feed(pane: &GhosttyPaneTerminal, sender: &mpsc::Sender<Bytes>, bytes: &[u8]) {
    let _ = pane.process_pty_bytes(PaneId::from_raw(1), 0, bytes, sender);
}

fn restore(snapshot: &[u8], core: PaneCoreHandover) -> GhosttyPaneTerminal {
    GhosttyPaneTerminal::from_handover(snapshot, core, SCROLLBACK_BYTES).expect("restore pane")
}

#[test]
fn pane_terminals_track_continuation() {
    // Without tracking, a terminal stopped mid-escape cannot be encoded.
    let mut untracked = Terminal::new(COLS, ROWS, SCROLLBACK_BYTES).expect("terminal");
    untracked.write(b"\x1b[3");
    assert!(untracked.encode_snapshot().is_err());

    let (pane, sender) = pane_terminal();
    feed(&pane, &sender, b"before \x1b[3");
    let (snapshot, core) = pane.encode_handover().expect("encode mid-escape pane");

    // A restored pane is a pane terminal too, and keeps tracking.
    let restored = restore(&snapshot, core);
    feed(&restored, &sender, b"1mred\x1b]2;half");
    let (snapshot, core) = restored
        .encode_handover()
        .expect("encode restored pane mid-escape");
    let restored = restore(&snapshot, core);
    feed(&restored, &sender, b" title\x07 after");
    assert_eq!(restored.terminal_title().as_deref(), Some("half title"));
    assert!(restored.visible_text().contains("before red after"));
}

fn rgb(r: u8, g: u8, b: u8) -> terminal_theme::RgbColor {
    terminal_theme::RgbColor { r, g, b }
}

fn theme(
    foreground: terminal_theme::RgbColor,
    background: terminal_theme::RgbColor,
) -> TerminalTheme {
    let mut theme = TerminalTheme {
        foreground: Some(foreground),
        background: Some(background),
        ..TerminalTheme::default()
    };
    theme.palette[2] = Some(rgb(0x0a, 0x0b, 0x0c));
    theme
}

fn pane_colors(pane: &GhosttyPaneTerminal) -> RenderColors {
    let (snapshot, _) = pane.encode_handover().expect("encode pane");
    let terminal =
        Terminal::decode_snapshot(&snapshot, PANE_CONTINUATION_MAX_BYTES).expect("decode pane");
    render_colors(&terminal)
}

#[test]
fn pane_wrapper_state_round_trips() {
    let (pane, sender) = pane_terminal();
    pane.apply_host_terminal_theme(theme(rgb(0xee, 0xee, 0xee), rgb(0x11, 0x11, 0x11)));
    let _ = pane.apply_host_terminal_appearance(Some(HostAppearance::Dark));
    feed(&pane, &sender, b"\x1b]0;agent title\x07\x1b]9;4;1;40\x07");
    feed(
        &pane,
        &sender,
        b"\x1b]10;rgb:12/34/56\x07\x1b]11;rgb:65/43/21\x07",
    );
    feed(&pane, &sender, b"\x1b[>1u\x1b[5 q");
    feed(&pane, &sender, b"prompt$ \x1b]2;split ti");

    let title = pane.terminal_title();
    let osc_title = pane.osc_title();
    let osc_progress = pane.osc_progress();
    let cursor_state = pane.cursor_state();
    let keyboard_protocol = pane.keyboard_protocol();
    let colors = pane_colors(&pane);
    assert_eq!(title.as_deref(), Some("agent title"));
    assert_eq!(osc_progress, "4;1;40");
    assert_eq!(cursor_state.map(|cursor| cursor.shape), Some(5));

    let (snapshot, core) = pane.encode_handover().expect("encode pane");
    let wire = serde_json::to_string(&core).expect("serialize wrapper state");
    let carried: PaneCoreHandover = serde_json::from_str(&wire).expect("parse wrapper state");
    assert_eq!(carried, core);

    let restored = restore(&snapshot, carried);
    assert_eq!(restored.terminal_title(), title);
    assert_eq!(restored.osc_title(), osc_title);
    assert_eq!(restored.osc_progress(), osc_progress);
    assert_eq!(restored.cursor_state(), cursor_state);
    assert_eq!(restored.keyboard_protocol(), keyboard_protocol);
    assert_eq!(pane_colors(&restored), colors);
    let (_, restored_core) = restored.encode_handover().expect("re-encode restored pane");
    assert_eq!(restored_core, core);

    feed(&restored, &sender, b"tle\x07");
    assert_eq!(restored.terminal_title().as_deref(), Some("split title"));

    // A later host theme update keeps the child's OSC 10/11 override.
    restored.apply_host_terminal_theme(theme(rgb(0xaa, 0xaa, 0xaa), rgb(0x22, 0x22, 0x22)));
    let updated = pane_colors(&restored);
    assert_eq!(
        updated.foreground,
        RgbColor {
            r: 0x12,
            g: 0x34,
            b: 0x56
        }
    );
    assert_eq!(
        updated.background,
        RgbColor {
            r: 0x65,
            g: 0x43,
            b: 0x21
        }
    );
}
