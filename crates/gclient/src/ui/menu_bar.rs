// upstream: none (Gobby's menu bar; herdr has no menu row)
//! The menu bar on row 0: seven titles, each opening its menu under itself.
//!
//! [`MenuBarMenu::ALL`] is the one definition of the title set and its
//! order; the menus' items live with the other menu lists in
//! `app::live_loop::menu`.

use crate::app::ContextMenuKind;
use crate::ui::chrome::Chrome;
use crate::ui::text::display_width_u16;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

/// A menu bar title, left to right.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MenuBarMenu {
    Gobby,
    File,
    Edit,
    View,
    Window,
    Agent,
    Help,
}

impl MenuBarMenu {
    /// Every title in bar order (the signed chrome canvas's set).
    pub const ALL: [MenuBarMenu; 7] = [
        MenuBarMenu::Gobby,
        MenuBarMenu::File,
        MenuBarMenu::Edit,
        MenuBarMenu::View,
        MenuBarMenu::Window,
        MenuBarMenu::Agent,
        MenuBarMenu::Help,
    ];

    pub fn title(self) -> &'static str {
        match self {
            MenuBarMenu::Gobby => "Gobby",
            MenuBarMenu::File => "File",
            MenuBarMenu::Edit => "Edit",
            MenuBarMenu::View => "View",
            MenuBarMenu::Window => "Window",
            MenuBarMenu::Agent => "Agent",
            MenuBarMenu::Help => "Help",
        }
    }
}

/// Each title's cell as drawn, by index into [`MenuBarMenu::ALL`].
#[derive(Debug, Clone, Default)]
pub struct MenuBarHits {
    pub titles: Vec<(usize, Rect)>,
}

/// One full-width row on the theme's header fill, the sidebar's section
/// header rows' colour, with their bold titles (`band_ink`). Each title's
/// cell pads it by one on either side, so neighbours read two cells apart
/// and the cells tile the bar from its left edge; the title whose menu is
/// open sits on the selection fill, like the active tab.
pub fn render_menu_bar(frame: &mut Frame, rect: Rect, chrome: &Chrome) -> MenuBarHits {
    let p = &chrome.palette;
    let bar = Style::new()
        .bg(p.band)
        .fg(p.band_ink.ink(p.subtext0))
        .add_modifier(Modifier::BOLD);
    // The title stays open while a submenu cascades from its menu.
    let root = std::iter::successors(chrome.menu.as_ref(), |menu| menu.parent.as_deref()).last();
    let open = root.and_then(|menu| match menu.kind {
        ContextMenuKind::MenuBar(open) => Some(open),
        _ => None,
    });
    let mut hits = MenuBarHits::default();
    let mut spans = Vec::with_capacity(MenuBarMenu::ALL.len());
    let mut x = rect.x;
    for (index, menu) in MenuBarMenu::ALL.into_iter().enumerate() {
        let label = format!(" {} ", menu.title());
        let width = display_width_u16(&label);
        let cell = Rect::new(x, rect.y, width, 1).intersection(rect);
        if !cell.is_empty() {
            hits.titles.push((index, cell));
        }
        let style = if open == Some(menu) {
            bar.bg(p.selection).fg(p.selection_ink.text)
        } else {
            bar
        };
        spans.push(Span::styled(label, style));
        x = x.saturating_add(width);
    }
    frame.render_widget(Paragraph::new(Line::from(spans)).style(bar), rect);
    hits
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::ContextMenuState;
    use crate::theme::{Theme, ThemeKind};
    use ratatui::backend::TestBackend;
    use ratatui::buffer::Buffer;
    use ratatui::Terminal;

    const WIDTH: u16 = 60;

    fn draw(chrome: &Chrome) -> (MenuBarHits, Buffer) {
        let mut terminal = Terminal::new(TestBackend::new(WIDTH, 2)).expect("test backend");
        let mut hits = MenuBarHits::default();
        terminal
            .draw(|frame| hits = render_menu_bar(frame, Rect::new(0, 0, WIDTH, 1), chrome))
            .expect("draw frame");
        (hits, terminal.backend().buffer().clone())
    }

    fn row_text(buffer: &Buffer, y: u16) -> String {
        (0..WIDTH).map(|x| buffer[(x, y)].symbol()).collect()
    }

    fn open(chrome: &mut Chrome, menu: MenuBarMenu) {
        chrome.menu = Some(ContextMenuState {
            kind: ContextMenuKind::MenuBar(menu),
            anchor: (0, 1),
            items: Vec::new(),
            selected: 0,
            item_rects: Vec::new(),
            parent: None,
        });
    }

    /// One full-width row on the header fill, the titles two cells apart,
    /// and each title's cell padded by one on either side so the cells tile
    /// the bar from its left edge.
    #[test]
    fn titles_render_on_row_zero_in_the_bar_colours() {
        let chrome = Chrome::dark();
        let palette = &chrome.palette;
        let (hits, buffer) = draw(&chrome);

        assert_eq!(
            row_text(&buffer, 0).trim_end(),
            " Gobby  File  Edit  View  Window  Agent  Help"
        );
        for x in 0..WIDTH {
            let cell = &buffer[(x, 0)];
            assert_eq!(
                (cell.fg, cell.bg),
                (palette.band_ink.ink(palette.subtext0), palette.band),
                "x={x}"
            );
            assert!(cell.modifier.contains(Modifier::BOLD), "x={x}");
        }
        assert_eq!(row_text(&buffer, 1).trim(), "", "the bar is one row");
        assert_eq!(
            hits.titles,
            vec![
                (0, Rect::new(0, 0, 7, 1)),
                (1, Rect::new(7, 0, 6, 1)),
                (2, Rect::new(13, 0, 6, 1)),
                (3, Rect::new(19, 0, 6, 1)),
                (4, Rect::new(25, 0, 8, 1)),
                (5, Rect::new(33, 0, 7, 1)),
                (6, Rect::new(40, 0, 6, 1)),
            ]
        );
    }

    /// The title whose menu is open sits on the selection fill across its
    /// whole cell, also while a submenu cascades from it; every other cell
    /// keeps the header fill.
    #[test]
    fn the_open_title_reads_in_the_open_colours() {
        let mut chrome = Chrome::new(Theme::new(ThemeKind::Light));
        open(&mut chrome, MenuBarMenu::View);
        let palette = chrome.palette;
        let (hits, buffer) = draw(&chrome);

        let view = hits
            .titles
            .iter()
            .find(|(index, _)| *index == 3)
            .map(|(_, rect)| *rect)
            .expect("View drawn");
        for x in 0..WIDTH {
            let cell = &buffer[(x, 0)];
            let expected = if (view.left()..view.right()).contains(&x) {
                (palette.selection_ink.text, palette.selection)
            } else {
                (palette.band_ink.ink(palette.subtext0), palette.band)
            };
            assert_eq!((cell.fg, cell.bg), expected, "x={x}");
        }

        // View › Theme open in front keeps View's title open.
        let view_menu = chrome.menu.take().expect("view menu");
        chrome.menu = Some(ContextMenuState {
            kind: ContextMenuKind::Submenu(crate::app::Submenu::Theme),
            anchor: (8, 1),
            items: Vec::new(),
            selected: 0,
            item_rects: Vec::new(),
            parent: Some(Box::new(view_menu)),
        });
        let (_, buffer) = draw(&chrome);
        let cell = &buffer[(view.x, 0)];
        assert_eq!(
            (cell.fg, cell.bg),
            (palette.selection_ink.text, palette.selection)
        );
    }
}
