use super::stripped_terminal_title;

#[test]
fn strips_one_recognized_leading_activity_glyph() {
    for title in ["⠋ task", "✳ task", "  ⠙   task  ", "✢ task", "✻ task"] {
        assert_eq!(stripped_terminal_title(title).as_deref(), Some("task"));
    }
    assert_eq!(
        stripped_terminal_title("⠋ ⠙ task").as_deref(),
        Some("⠙ task")
    );
}

#[test]
fn preserves_unrecognized_or_unbounded_symbols() {
    for (title, expected) in [
        ("★task", "★task"),
        ("★ production", "★ production"),
        ("✨ task", "✨ task"),
        ("☼ status", "☼ status"),
        ("@ task", "@ task"),
        ("task ⠋ detail", "task ⠋ detail"),
        ("[prod] task", "[prod] task"),
    ] {
        assert_eq!(stripped_terminal_title(title).as_deref(), Some(expected));
    }
}

#[test]
fn preserves_unicode_text_and_elides_empty_results() {
    assert_eq!(
        stripped_terminal_title(" ⠋ 修复🙂标题 ").as_deref(),
        Some("修复🙂标题")
    );
    assert_eq!(stripped_terminal_title("  "), None);
    assert_eq!(stripped_terminal_title("⠋   "), None);
}
