use super::*;

fn cursor(x: u16, y: u16, visible: bool, shape: u8) -> TerminalCursorState {
    TerminalCursorState {
        x,
        y,
        visible,
        shape,
    }
}

#[test]
fn cursor_settle_holds_position_change_until_quiet_window() {
    let now = Instant::now();
    let mut settle = CursorPositionSettleState::default();
    settle.observe(Some(cursor(1, 0, true, 0)), now);
    settle.observe(Some(cursor(20, 5, true, 0)), now + Duration::from_millis(1));

    let reported = settle
        .reported_cursor(Some(cursor(20, 5, true, 0)), now + Duration::from_millis(2))
        .unwrap();

    assert_eq!((reported.x, reported.y), (1, 0));
}

#[test]
fn cursor_settle_adopts_position_change_after_quiet_window() {
    let now = Instant::now();
    let mut settle = CursorPositionSettleState::default();
    settle.observe(Some(cursor(1, 0, true, 0)), now);
    settle.observe(Some(cursor(2, 0, true, 0)), now + Duration::from_millis(1));

    let reported = settle
        .reported_cursor(
            Some(cursor(2, 0, true, 0)),
            now + CURSOR_POSITION_SETTLE + Duration::from_millis(1),
        )
        .unwrap();

    assert_eq!((reported.x, reported.y), (2, 0));
}

#[test]
fn cursor_settle_caps_continuous_position_changes_from_first_pending_time() {
    let now = Instant::now();
    let mut settle = CursorPositionSettleState::default();
    settle.observe(Some(cursor(1, 0, true, 0)), now);
    settle.observe(Some(cursor(2, 0, true, 0)), now + Duration::from_millis(1));
    settle.observe(
        Some(cursor(3, 0, true, 0)),
        now + CURSOR_POSITION_MAX_HOLD + Duration::from_millis(1),
    );

    assert!(!settle.pending());
    assert_eq!(
        settle.reported_cursor(
            Some(cursor(3, 0, true, 0)),
            now + CURSOR_POSITION_MAX_HOLD + Duration::from_millis(2),
        ),
        Some(cursor(3, 0, true, 0))
    );
}

#[test]
fn cursor_settle_keeps_render_read_pure() {
    let now = Instant::now();
    let mut settle = CursorPositionSettleState::default();
    settle.observe(Some(cursor(1, 0, true, 0)), now);
    settle.observe(Some(cursor(2, 0, true, 0)), now + Duration::from_millis(1));

    assert!(settle.pending());
    let _ = settle.reported_cursor(
        Some(cursor(2, 0, true, 0)),
        now + CURSOR_POSITION_SETTLE + Duration::from_millis(1),
    );

    assert!(settle.pending());
}

#[test]
fn cursor_settle_passes_shape_through_while_position_is_held() {
    let now = Instant::now();
    let mut settle = CursorPositionSettleState::default();
    settle.observe(Some(cursor(1, 0, true, 2)), now);
    settle.observe(Some(cursor(2, 0, true, 6)), now + Duration::from_millis(1));

    let reported = settle
        .reported_cursor(Some(cursor(2, 0, true, 6)), now + Duration::from_millis(2))
        .unwrap();

    assert_eq!((reported.x, reported.y, reported.shape), (1, 0, 6));
}

#[test]
fn cursor_settle_passes_shape_through_after_quiet_window() {
    let now = Instant::now();
    let mut settle = CursorPositionSettleState::default();
    settle.observe(Some(cursor(1, 0, true, 2)), now);
    settle.observe(Some(cursor(2, 0, true, 2)), now + Duration::from_millis(1));

    let reported = settle
        .reported_cursor(
            Some(cursor(2, 0, true, 6)),
            now + CURSOR_POSITION_SETTLE + Duration::from_millis(1),
        )
        .unwrap();

    assert_eq!((reported.x, reported.y, reported.shape), (2, 0, 6));
}

#[test]
fn cursor_settle_hides_immediately_and_waits_to_reveal() {
    let now = Instant::now();
    let mut settle = CursorPositionSettleState::default();
    settle.observe(Some(cursor(1, 0, true, 0)), now);
    settle.observe(Some(cursor(1, 0, false, 0)), now + Duration::from_millis(1));

    assert_eq!(
        settle.reported_cursor(Some(cursor(1, 0, false, 0)), now + Duration::from_millis(2)),
        Some(cursor(1, 0, false, 0))
    );

    settle.observe(Some(cursor(1, 0, true, 0)), now + Duration::from_millis(3));
    assert_eq!(
        settle.reported_cursor(Some(cursor(1, 0, true, 0)), now + Duration::from_millis(4)),
        Some(cursor(1, 0, false, 0))
    );
}
