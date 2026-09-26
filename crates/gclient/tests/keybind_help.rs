use gobby_client::ui::keybind_help::{help_lines, help_rows, render_keybind_help};
use gobby_client::ui::Chrome;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;

fn rendered_help(chrome: &Chrome, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).expect("test terminal");
    terminal
        .draw(|frame| {
            render_keybind_help(frame, Rect::new(0, 0, width, height), chrome);
        })
        .expect("render keybind help");
    let buffer = terminal.backend().buffer();
    (0..height)
        .map(|y| {
            (0..width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn compact(value: &str) -> String {
    value
        .chars()
        .filter(|ch| !ch.is_whitespace() && !matches!(ch, '│' | '┌' | '┐' | '└' | '┘' | '─' | '▐'))
        .collect()
}

fn line_text(line: &ratatui::text::Line<'_>) -> String {
    line.spans
        .iter()
        .map(|span| span.content.as_ref())
        .collect()
}

#[test]
fn the_description_column_is_never_cut_at_narrow_widths() {
    let mut chrome = Chrome::dark();
    for (width, height) in [(120, 40), (80, 30), (56, 24), (26, 24)] {
        for entry in chrome.keymap.help_entries() {
            chrome.keybind_help.query = entry.name.to_owned();
            let screen = rendered_help(&chrome, width, height);
            assert!(
                compact(&screen).contains(&compact(entry.description)),
                "{} description missing at {width}×{height}: {screen}",
                entry.name
            );
        }
    }
}

#[test]
fn the_keymap_name_draws_only_when_the_longest_row_fits() {
    let chrome = Chrome::dark();
    let names: Vec<_> = std::iter::once("prefix")
        .chain(chrome.keymap.help_entries().iter().map(|entry| entry.name))
        .collect();
    let wide = help_rows(&chrome, u16::MAX);
    let longest = wide
        .iter()
        .map(|line| line_text(line).chars().count())
        .max()
        .unwrap();
    let fitting = help_rows(&chrome, longest as u16);
    let narrow = help_rows(&chrome, (longest - 1) as u16);
    assert_eq!(
        fitting.len(),
        narrow.len(),
        "hiding names must not add rows"
    );
    assert_eq!(fitting.len(), names.len());
    for ((with_name, without_name), name) in fitting.iter().zip(&narrow).zip(names) {
        assert!(line_text(with_name).ends_with(&format!("({name})")));
        assert!(!line_text(without_name).ends_with(&format!("({name})")));
    }
    assert!(
        help_rows(&chrome, 56)
            .iter()
            .all(|line| !line_text(line).contains("(move_tab_left)")),
        "a narrow overlay hides the name column"
    );
}

#[test]
fn name_stays_searchable_when_hidden() {
    let mut chrome = Chrome::dark();
    chrome.keybind_help.query = "move_tab_left".to_owned();
    let rows = help_rows(&chrome, 40);
    let text = rows.iter().map(line_text).collect::<Vec<_>>().join("\n");
    assert!(
        compact(&text).contains(&compact("Move the active tab left")),
        "{text}"
    );
    assert!(!text.contains("(move_tab_left)"), "{text}");
}

#[test]
fn wrapped_rows_keep_the_last_binding_reachable_by_logical_scroll() {
    let mut chrome = Chrome::dark();
    let last = chrome.keymap.help_entries().pop().expect("last binding");
    chrome.keybind_help.scroll = help_lines(&chrome).len() - 1;
    let screen = rendered_help(&chrome, 56, 24);
    assert!(
        compact(&screen).contains(&compact(last.description)),
        "{} is not visible at the end of scroll: {screen}",
        last.name
    );
}

#[test]
fn narrow_footer_keeps_the_close_and_back_actions_visible() {
    let mut chrome = Chrome::dark();
    for (width, height) in [(56, 24), (26, 24)] {
        let screen = rendered_help(&chrome, width, height);
        let footer = screen
            .lines()
            .nth((height - 3) as usize)
            .expect("footer row");
        assert!(footer.contains("esc close"), "{width} columns: {footer}");

        chrome.keybind_help.search_focused = true;
        let screen = rendered_help(&chrome, width, height);
        let footer = screen
            .lines()
            .nth((height - 3) as usize)
            .expect("footer row");
        assert!(footer.contains("esc back"), "{width} columns: {footer}");
        chrome.keybind_help.search_focused = false;
    }
}
