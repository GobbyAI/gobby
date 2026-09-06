//! herdr `src/ui/tabs.rs` (5) keep-set render tests.

use gobby_client::app::{PaneId, Workspace};
use gobby_client::ui::chrome::Tab;
use gobby_client::ui::tabs::{render_tab_bar, tab_display_name, tab_width, TabBarHits};
use gobby_client::ui::text::display_width_u16;
use gobby_client::ui::Chrome;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::style::Modifier;
use ratatui::Terminal;

use super::fixtures::{cell, draw, rect_rows, terminal};
use super::token_map::{palette, theme};

/// herdr `Workspace::test_new("test")`: one auto-named tab. gclient keeps tabs
/// on `Chrome`, with an empty title marking a tab as auto-named.
fn chrome_with_one_tab() -> Chrome {
    let mut chrome = Chrome::new(theme());
    chrome.tabs.push(Tab::new("", PaneId(1)));
    chrome
}

/// herdr `ws.test_add_tab(Some(name))`: returns the new tab's index.
fn add_tab(chrome: &mut Chrome, name: Option<&str>) -> usize {
    let idx = chrome.tabs.len();
    chrome
        .tabs
        .push(Tab::new(name.unwrap_or(""), PaneId(idx as u32 + 1)));
    idx
}

/// herdr `tabs[i].set_custom_name(name)`.
fn set_custom_name(chrome: &mut Chrome, idx: usize, name: &str) {
    chrome.tabs[idx].title = name.to_string();
}

/// Draws the tab bar into `rect` and returns the terminal plus hit areas.
fn draw_tab_bar(chrome: &Chrome, rect: Rect) -> (Terminal<TestBackend>, TabBarHits) {
    let ws = Workspace::scripted();
    let mut term = terminal(rect.x + rect.width, rect.y + rect.height);
    let mut hits = TabBarHits::default();
    draw(&mut term, |frame| {
        hits = render_tab_bar(frame, rect, &ws, chrome);
    });
    (term, hits)
}

/// herdr `buffer_row_text`: the row inside `area`, trailing spaces trimmed.
fn buffer_row_text(term: &Terminal<TestBackend>, area: Rect, row: u16) -> String {
    let rows = rect_rows(term, Rect::new(area.x, row, area.width, 1));
    rows.into_iter()
        .next()
        .expect("row inside area")
        .trim_end()
        .to_string()
}

parity_tests! {
    "src/ui/tabs.rs" => {
        fn tab_bar_marks_zoomed_tabs_without_renaming_them() {
            let mut chrome = chrome_with_one_tab();
            chrome.tabs[0].zoomed = true;
            let custom_tab = add_tab(&mut chrome, Some("test"));
            chrome.tabs[custom_tab].zoomed = true;

            let tab_bar_rect = Rect::new(0, 0, 30, 1);
            let (term, _) = draw_tab_bar(&chrome, tab_bar_rect);

            let row = buffer_row_text(&term, tab_bar_rect, 0);
            assert!(row.contains(" 1 Z"), "tab row: {row:?}");
            assert!(row.contains(" test Z"), "tab row: {row:?}");
            assert_eq!(tab_display_name(&chrome.tabs, 0).as_deref(), Some("1"));
            assert_eq!(
                tab_display_name(&chrome.tabs, custom_tab).as_deref(),
                Some("test")
            );
        }

        fn active_auto_named_tab_keeps_readable_weight() {
            let chrome = chrome_with_one_tab();

            let tab_bar_rect = Rect::new(0, 0, 30, 1);
            let (term, hits) = draw_tab_bar(&chrome, tab_bar_rect);

            let (_, tab_rect) = hits.tabs[0];
            let style = cell(&term, tab_rect.x + 1, tab_rect.y).style();

            assert_eq!(style.bg, Some(palette().accent));
            assert!(!style.add_modifier.contains(Modifier::DIM));
            assert!(!style.add_modifier.contains(Modifier::BOLD));
        }

        fn zoom_marker_counts_toward_tab_width() {
            let mut chrome = chrome_with_one_tab();
            set_custom_name(&mut chrome, 0, "abcdefgh");
            chrome.tabs[0].zoomed = true;

            assert_eq!(tab_width(&chrome.tabs, 0), 14);
        }

        fn tab_width_uses_display_width_for_cjk_labels() {
            let mut chrome = chrome_with_one_tab();
            set_custom_name(&mut chrome, 0, "提交 herdr 的反馈");

            assert_eq!(
                tab_width(&chrome.tabs, 0),
                display_width_u16("提交 herdr 的反馈") + 4
            );
        }

        fn tab_bar_renders_trailing_cjk_character() {
            let mut chrome = chrome_with_one_tab();
            set_custom_name(&mut chrome, 0, "提交 herdr 的反馈");

            let tab_bar_rect = Rect::new(0, 0, 30, 1);
            let (term, _) = draw_tab_bar(&chrome, tab_bar_rect);

            let row = buffer_row_text(&term, tab_bar_rect, 0);
            assert!(row.contains('馈'), "tab row: {row:?}");
        }
    }
}
