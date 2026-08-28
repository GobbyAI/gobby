use super::*;

fn make_sel(sr: u32, sc: u16, er: u32, ec: u16) -> Selection {
    let mut sel = Selection::anchor(PaneId::from_raw(0), sr as u16, sc, None);
    sel.anchor = (sr, sc);
    sel.cursor = (er, ec);
    sel.phase = Phase::Dragging;
    sel
}

#[test]
fn osc52_sequence_uses_bel_terminator() {
    assert_eq!(osc52_sequence(b"hello"), "\x1b]52;c;aGVsbG8=\x07");
}

#[test]
fn ssh_sessions_prefer_osc52() {
    assert!(should_prefer_osc52_for_env(
        Some(OsStr::new("1 2 3 4")),
        None,
        None,
        false
    ));
    assert!(should_prefer_osc52_for_env(
        None,
        Some(OsStr::new("/dev/ttys001")),
        None,
        false
    ));
    assert!(!should_prefer_osc52_for_env(None, None, None, false));
}

#[test]
fn wsl_sessions_prefer_osc52() {
    assert!(should_prefer_osc52_for_env(None, None, None, true));
}

#[test]
fn vscode_remote_sessions_prefer_osc52() {
    assert!(should_prefer_osc52_for_env(
        None,
        None,
        Some(OsStr::new("/tmp/vscode-remote-cli.sock")),
        false
    ));
}

#[test]
fn wsl_detection_uses_env_vars() {
    assert!(is_wsl_for_env(
        None,
        None,
        Some(OsStr::new("Ubuntu")),
        None,
        false
    ));
    assert!(is_wsl_for_env(
        None,
        None,
        None,
        Some(OsStr::new("/run/WSL/123_interop")),
        false
    ));
}

#[test]
fn wsl_detection_uses_kernel_markers() {
    assert!(is_wsl_for_env(
        Some("5.15.167.4-microsoft-standard-WSL2"),
        None,
        None,
        None,
        false
    ));
    assert!(is_wsl_for_env(
        None,
        Some("Linux version 5.15.167.4-microsoft-standard-WSL2"),
        None,
        None,
        false
    ));
}

#[test]
fn wsl_detection_ignores_non_wsl_kernel_strings() {
    assert!(!contains_wsl_marker("notwsl-kernel"));
    assert!(!is_wsl_for_env(
        Some("6.8.0-31-generic"),
        Some("Linux version 6.8.0-31-generic"),
        None,
        None,
        false
    ));
}

#[test]
fn wsl_detection_uses_wsl_runtime_markers() {
    assert!(is_wsl_for_env(None, None, None, None, true));
    assert!(!is_wsl_for_env(None, None, None, None, false));
}

#[test]
fn ordering_forward() {
    let sel = make_sel(2, 5, 4, 10);
    assert_eq!(sel.ordered(), ((2, 5), (4, 10)));
}

#[test]
fn ordering_backward() {
    let sel = make_sel(4, 10, 2, 5);
    assert_eq!(sel.ordered(), ((2, 5), (4, 10)));
}

#[test]
fn single_line_contains() {
    let sel = make_sel(2, 5, 2, 15);
    assert!(!sel.contains(2, 4, None));
    assert!(sel.contains(2, 5, None));
    assert!(sel.contains(2, 10, None));
    assert!(sel.contains(2, 15, None));
    assert!(!sel.contains(2, 16, None));
    assert!(!sel.contains(1, 10, None));
    assert!(!sel.contains(3, 10, None));
}

#[test]
fn multi_line_contains() {
    let sel = make_sel(2, 5, 4, 10);
    assert!(!sel.contains(2, 4, None));
    assert!(sel.contains(2, 5, None));
    assert!(sel.contains(2, 79, None));
    assert!(sel.contains(3, 0, None));
    assert!(sel.contains(3, 79, None));
    assert!(sel.contains(4, 0, None));
    assert!(sel.contains(4, 10, None));
    assert!(!sel.contains(4, 11, None));
}

#[test]
fn anchored_not_visible() {
    let sel = Selection::anchor(PaneId::from_raw(0), 5, 10, None);
    assert!(!sel.is_visible());
    assert!(!sel.contains(5, 10, None));
}

#[test]
fn click_without_drag() {
    let mut sel = Selection::anchor(PaneId::from_raw(0), 5, 10, None);
    assert!(sel.was_just_click());
    let copied = sel.finish();
    assert!(!copied);
}

#[test]
fn drag_then_finish() {
    let mut sel = Selection::anchor(PaneId::from_raw(0), 5, 10, None);
    sel.drag(20, 7, Rect::new(10, 5, 80, 24), None);
    assert!(sel.is_visible());
    assert!(!sel.was_just_click());
    let copied = sel.finish();
    assert!(copied);
}

#[test]
fn drag_uses_buffer_rows_when_scrolled() {
    let mut sel = Selection::anchor(
        PaneId::from_raw(0),
        0,
        10,
        Some(ScrollMetrics {
            offset_from_bottom: 1,
            max_offset_from_bottom: 10,
            viewport_rows: 4,
        }),
    );

    sel.drag(
        10,
        5,
        Rect::new(10, 5, 80, 4),
        Some(ScrollMetrics {
            offset_from_bottom: 2,
            max_offset_from_bottom: 10,
            viewport_rows: 4,
        }),
    );

    assert_eq!(sel.ordered_cells(), ((8, 0), (9, 10)));
}

#[test]
fn contains_tracks_current_viewport_after_scroll() {
    let sel = make_sel(8, 2, 10, 4);
    let metrics = Some(ScrollMetrics {
        offset_from_bottom: 2,
        max_offset_from_bottom: 10,
        viewport_rows: 4,
    });

    assert!(sel.contains(0, 2, metrics));
    assert!(sel.contains(1, 40, metrics));
    assert!(sel.contains(2, 4, metrics));
    assert!(!sel.contains(3, 4, metrics));
}

#[test]
fn clamp_to_pane_bounds() {
    let (row, col) = clamp_to_pane(200, 100, Rect::new(10, 5, 80, 24));
    assert_eq!(row, 23);
    assert_eq!(col, 79);

    let (row, col) = clamp_to_pane(0, 0, Rect::new(10, 5, 80, 24));
    assert_eq!(row, 0);
    assert_eq!(col, 0);
}

#[test]
fn anchor_screen_pos_adds_pane_origin() {
    // Pane offset by sidebar (x=10) and tab bar (y=5).
    // Anchor at viewport_row=3, col=5 (pane-relative).
    let sel = Selection::anchor(PaneId::from_raw(0), 3, 5, None);
    let pane_inner = Rect::new(10, 5, 80, 24);
    let (row, col) = sel.anchor_screen_pos(pane_inner, None);
    // Screen row = 3 + 5 = 8, screen col = 5 + 10 = 15
    assert_eq!(row, 8);
    assert_eq!(col, 15);
}

#[test]
fn anchor_screen_pos_same_cell_as_mouse_with_offset() {
    // When the pane has a non-zero origin, anchor and mouse on the same
    // screen cell must compare equal — no false drag detection.
    let pane_inner = Rect::new(10, 5, 80, 24);
    // Mouse clicked at screen (15, 8) → anchor stored as (viewport_row=3, col=5)
    let sel = Selection::anchor(PaneId::from_raw(0), 3, 5, None);
    let (ar, ac) = sel.anchor_screen_pos(pane_inner, None);
    // Screen position of the anchor must match the mouse position
    assert_eq!((ar, ac), (8, 15));
}
