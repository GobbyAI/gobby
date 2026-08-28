use super::*;

#[test]
fn host_keyboard_report_all_only_changes_the_current_gterm_stack_entry() {
    let mut output = Vec::new();

    set_host_kitty_keyboard_report_all(&mut output, true).unwrap();
    set_host_kitty_keyboard_report_all(&mut output, false).unwrap();

    assert_eq!(output, b"\x1b[=15u\x1b[=7u");
}

#[test]
fn clears_all_known_host_mouse_modes() {
    let sequence = std::str::from_utf8(DISABLE_HOST_MOUSE_REPORTING_SEQUENCE).unwrap();

    for mode in ["1000", "1002", "1003", "1005", "1006", "1015", "1016"] {
        assert!(
            sequence.contains(&format!("\x1b[?{mode}l")),
            "missing mouse mode {mode}"
        );
    }
}
