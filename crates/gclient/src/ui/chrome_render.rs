// upstream: herdr v0.8.0 src/ui.rs
//! Frame composition (herdr `render`): the splash alone until the first
//! frame lands, else menu bar, tab bar, tab surface or empty state,
//! sidebar and notifications; then the mode overlay on either.

use crate::app::PaneId;
use crate::ui::chrome::{Chrome, Mode, WorkspaceView};
use crate::ui::menu_bar::MenuBarHits;
use crate::ui::panes::PaneContent;
use crate::ui::settings::SettingsHits;
use crate::ui::sidebar::SidebarHits;
use crate::ui::tabs::TabBarHits;
use crate::ui::{
    context_menu, dialogs, keybind_help, menu_bar, navigator, pane_chrome, panes, settings,
    sidebar, splash, status, tab_surface,
};
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};
use ratatui::widgets::Clear;
use ratatui::Frame;

/// Every rect the chrome renderers drew this frame; the run loop writes it
/// back with `Chrome::apply_hits` so hit tests match the screen.
#[derive(Debug, Clone, Default)]
pub struct ChromeHits {
    pub menu_bar: MenuBarHits,
    pub tab_bar: TabBarHits,
    pub sidebar: SidebarHits,
    pub control_indicator: Option<Rect>,
    pub status_count: Option<Rect>,
    pub toast: Option<Rect>,
    pub settings: Option<SettingsHits>,
    /// Rows of the context menu and each menu it cascades from as drawn,
    /// one list per menu in item order, the open menu's first, whenever the
    /// menu overlay ran; `Chrome::apply_hits` writes them into those menus.
    pub menu_rows: Option<Vec<Vec<Rect>>>,
    /// Buttons of the open dialog as drawn, in its button order.
    pub dialog_buttons: Vec<Rect>,
    /// The furthest scroll the keybinding help's drawn body shows.
    pub help_last_scroll: usize,
    /// The open keybinding help, navigator, Alerts, Open worktree or Destroy
    /// orphaned terminals popup.
    pub dialog: Option<Rect>,
}

/// Compose the whole frame; `content` paints each pane's terminal grid.
pub fn render_workspace_with<W: WorkspaceView>(
    frame: &mut Frame,
    ws: &W,
    chrome: &Chrome,
    content: &mut PaneContent<'_>,
) -> ChromeHits {
    let area = frame.area();
    // Nothing fills the frame first: `paint_ground` settles what the default
    // colours mean once everything is drawn.
    let mut hits = if splashing(ws, chrome) {
        splash::render_splash(frame, area, chrome);
        ChromeHits::default()
    } else {
        render_chrome(frame, ws, chrome, content)
    };

    let terminal_area = chrome.view.terminal_area;
    let close_area = if terminal_area.is_empty() {
        area
    } else {
        terminal_area
    };
    match chrome.mode {
        Mode::ConfirmClose => {
            hits.dialog_buttons = render_dialog_overlay(frame, close_area, chrome);
        }
        Mode::Rename | Mode::Respond | Mode::ProjectDialog => {
            hits.dialog_buttons = render_dialog_overlay(frame, area, chrome);
        }
        Mode::Settings => {
            dim_background(frame, area);
            hits.settings = settings::render_settings(frame, area, chrome);
            hits.dialog_buttons = hits
                .settings
                .as_ref()
                .map(|settings| settings.buttons.clone())
                .unwrap_or_default();
        }
        Mode::KeybindHelp => {
            dim_background(frame, area);
            (hits.dialog_buttons, hits.help_last_scroll) =
                keybind_help::render_keybind_help(frame, area, chrome);
        }
        Mode::Navigator => {
            dim_background(frame, area);
            navigator::render_navigator(frame, area, ws, chrome);
        }
        // Composited last and over an undimmed workspace: the menu is
        // contextual, so what it acts on stays readable.
        Mode::ContextMenu => {
            hits.menu_rows = Some(context_menu::render_context_menu(frame, area, chrome));
        }
        Mode::Terminal | Mode::Navigate | Mode::Prefix | Mode::Copy | Mode::Resize => {}
    }
    hits.dialog = match (chrome.mode, &chrome.dialog) {
        (Mode::KeybindHelp, _) => keybind_help::popup_area(area),
        (Mode::Navigator, _) => navigator::popup_area(area),
        (Mode::ProjectDialog, Some(dialogs::Dialog::Alerts { .. })) => {
            dialogs::alerts::popup_area(area, chrome)
        }
        (Mode::ProjectDialog, Some(dialogs::Dialog::OpenWorktree { choices, .. })) => {
            dialogs::project::open_worktree_area(area, choices.len())
        }
        (Mode::ProjectDialog, Some(dialogs::Dialog::DestroyOrphans { rows, .. })) => {
            dialogs::orphans::popup_area(area, rows.len())
        }
        _ => None,
    };
    paint_ground(frame, chrome);
    hits
}

/// Light and Dark own the ground: every cell still on the terminal's default
/// colours, hosted panes included, takes the theme's text on `panel_bg`, so
/// the hosting terminal's configured background and foreground no longer show
/// through; host opacity that applies to explicit cells still does. System
/// leaves the terminal's own colours in place. Runs last: overlays `Clear`
/// their cells back to the default.
fn paint_ground(frame: &mut Frame, chrome: &Chrome) {
    if chrome.prefs.follows_system() {
        return;
    }
    let p = &chrome.palette;
    for cell in &mut frame.buffer_mut().content {
        if cell.fg == Color::Reset {
            cell.fg = p.text;
        }
        if cell.bg == Color::Reset {
            cell.bg = p.panel_bg;
        }
    }
}

/// Until startup draws its first frame, the goblin and the wordmark stand alone
/// on the ground: no bar, tabs, sidebar, status or toasts. A first connect
/// that failed (a retry scheduled, or a daemon error) never finishes its
/// stages, so it falls through to the chrome, whose status line says what
/// went wrong.
fn splashing<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> bool {
    chrome
        .connection
        .stages
        .as_ref()
        .is_some_and(|stages| !stages.finished())
        && chrome.connection.retry_at.is_none()
        && ws.daemon_error().is_none()
}

/// The menu bar over a row of bare ground, the content column, the sidebar
/// over both, then the status line and the toasts.
fn render_chrome<W: WorkspaceView>(
    frame: &mut Frame,
    ws: &W,
    chrome: &Chrome,
    content: &mut PaneContent<'_>,
) -> ChromeHits {
    let menu_bar = menu_bar::render_menu_bar(frame, chrome.view.menu_bar_rect, chrome);
    // The sidebar after the content: the overlay lies over it.
    let tab_bar = render_content_column(frame, ws, chrome, content);
    let sidebar = render_navigation_chrome(frame, ws, chrome);
    let mut hits = ChromeHits {
        menu_bar,
        tab_bar,
        sidebar,
        ..ChromeHits::default()
    };

    let status_rect = chrome.view.status_rect;
    let status_hits = if status_rect.is_empty() {
        status::StatusHits::default()
    } else {
        status::render_status_line(frame, status_rect, ws, chrome)
    };
    hits.control_indicator = status_hits
        .control_indicator
        .or_else(|| pane_chrome::control_indicator_hit_area(ws, chrome));
    hits.status_count = status_hits.count;

    // Ambient notifications sit above panes, but below interactive overlays.
    hits.toast = render_notifications(frame, chrome);
    hits
}

/// Compose the whole frame with empty pane bodies.
pub fn render_workspace<W: WorkspaceView>(
    frame: &mut Frame,
    ws: &W,
    chrome: &Chrome,
) -> ChromeHits {
    let mut none = |_: &mut Frame, _: Rect, _: PaneId| {};
    render_workspace_with(frame, ws, chrome, &mut none)
}

/// herdr `render_navigation_chrome`: the sidebar, pinned as a column or as
/// the overlay. Hit areas are returned by the sidebar; the run loop stores
/// them.
fn render_navigation_chrome<W: WorkspaceView>(
    frame: &mut Frame,
    ws: &W,
    chrome: &Chrome,
) -> SidebarHits {
    let rect = chrome.view.sidebar_rect;
    if rect.width == 0 {
        return SidebarHits::default();
    }
    // The sidebar's panel only sets colours, so the overlay first wipes the
    // pane glyphs under it; the pinned column holds none.
    frame.render_widget(Clear, rect);
    sidebar::render_sidebar(frame, rect, ws, chrome)
}

/// Tab bar row plus terminal area: the active tab's surface, or the empty
/// state when no tab is open. Skipped when the terminal area has no cells.
fn render_content_column<W: WorkspaceView>(
    frame: &mut Frame,
    ws: &W,
    chrome: &Chrome,
    content: &mut PaneContent<'_>,
) -> TabBarHits {
    let terminal_area = chrome.view.terminal_area;
    if terminal_area.is_empty() {
        return TabBarHits::default();
    }
    if chrome.tabs().tabs.is_empty() {
        panes::render_empty(frame, terminal_area, chrome);
        return TabBarHits::default();
    }
    let surface = chrome
        .view
        .tab_bar_rect
        .map_or(terminal_area, |tabs| tabs.union(terminal_area));
    tab_surface::render_tab_surface(frame, surface, ws, chrome, content)
}

/// herdr `render_notifications`: the toast stack over the terminal area.
/// Returns the union of the toasts it drew, when it drew any.
fn render_notifications(frame: &mut Frame, chrome: &Chrome) -> Option<Rect> {
    // Toasts sit over the pane area (D3); the frame stands in before a
    // layout has run.
    let terminal_area = chrome.view.terminal_area;
    let area = if terminal_area.is_empty() {
        frame.area()
    } else {
        // Keep notifications off the pane's top and right border cells.
        Rect::new(
            terminal_area.x.saturating_add(1),
            terminal_area.y.saturating_add(1),
            terminal_area.width.saturating_sub(2),
            terminal_area.height.saturating_sub(2),
        )
    };
    status::render_toast_notification(frame, area, chrome)
}

/// Dim `area` and draw the pending dialog over it, returning the button
/// rects it drew; nothing when no dialog is set.
fn render_dialog_overlay(frame: &mut Frame, area: Rect, chrome: &Chrome) -> Vec<Rect> {
    if chrome.dialog.is_none() {
        return Vec::new();
    }
    dim_background(frame, area);
    dialogs::render_dialog(frame, area, chrome)
}

/// herdr `dim_background`: DIM modifier over `area`.
pub fn dim_background(frame: &mut Frame, area: Rect) {
    let buf = frame.buffer_mut();
    let area = area.intersection(buf.area);
    for y in area.y..area.y + area.height {
        for x in area.x..area.x + area.width {
            let cell = &mut buf[(x, y)];
            cell.set_style(cell.style().add_modifier(Modifier::DIM));
        }
    }
}

/// herdr `copy_feedback_offset_for_toast`: rows the copy-feedback box lifts
/// by so it clears a toast it would otherwise overlap; `base_offset` when the
/// two rects are apart.
pub fn copy_feedback_offset_for_toast(
    feedback_rect: Rect,
    base_offset: u16,
    toast_rect: Rect,
) -> u16 {
    if rects_overlap(feedback_rect, toast_rect) {
        base_offset.saturating_add(toast_rect.height)
    } else {
        base_offset
    }
}

/// herdr `rects_overlap`.
pub fn rects_overlap(a: Rect, b: Rect) -> bool {
    a.x < b.x.saturating_add(b.width)
        && b.x < a.x.saturating_add(a.width)
        && a.y < b.y.saturating_add(b.height)
        && b.y < a.y.saturating_add(a.height)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::ThemeKind;
    use crate::ui::status::Toast;
    use gobby_terminal::terminal_theme::{DefaultColorKind, RgbColor};
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    #[test]
    fn rects_overlap_cases() {
        let a = Rect::new(0, 0, 4, 4);
        assert!(rects_overlap(a, a));
        assert!(rects_overlap(a, Rect::new(2, 2, 4, 4)));
        assert!(rects_overlap(a, Rect::new(3, 3, 1, 1)));
        assert!(
            !rects_overlap(a, Rect::new(4, 0, 2, 2)),
            "edge-adjacent on x"
        );
        assert!(
            !rects_overlap(a, Rect::new(0, 4, 2, 2)),
            "edge-adjacent on y"
        );
        assert!(!rects_overlap(a, Rect::new(10, 10, 1, 1)));
        // herdr semantics: a zero-size rect strictly inside another overlaps
        // it; two zero-size rects never do.
        assert!(rects_overlap(a, Rect::new(1, 1, 0, 0)));
        assert!(!rects_overlap(Rect::new(0, 0, 0, 0), Rect::new(0, 0, 0, 0)));
        assert!(!rects_overlap(a, Rect::new(4, 4, 0, 0)));
    }

    #[test]
    fn notifications_leave_the_pane_border_visible() {
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        let mut chrome = Chrome::dark();
        chrome.view.terminal_area = Rect::new(0, 1, 80, 22);
        chrome.notify(Toast::info("connected"));
        let mut notification = None;
        terminal
            .draw(|frame| notification = render_notifications(frame, &chrome))
            .unwrap();
        let rect = notification.expect("toast drawn");
        assert!(rect.y > chrome.view.terminal_area.y);
        assert!(rect.right() < chrome.view.terminal_area.right());
    }

    #[test]
    fn dim_background_marks_every_cell_in_area() {
        let mut terminal = Terminal::new(TestBackend::new(6, 4)).unwrap();
        let area = Rect::new(1, 1, 3, 2);
        terminal.draw(|frame| dim_background(frame, area)).unwrap();
        let buffer = terminal.backend().buffer();
        for y in 0..4 {
            for x in 0..6 {
                let dimmed = buffer[(x, y)].modifier.contains(Modifier::DIM);
                let inside = rects_overlap(area, Rect::new(x, y, 1, 1));
                assert_eq!(dimmed, inside, "cell ({x}, {y})");
            }
        }
    }

    #[test]
    fn dim_background_clips_to_the_frame() {
        let mut terminal = Terminal::new(TestBackend::new(4, 3)).unwrap();
        terminal
            .draw(|frame| dim_background(frame, Rect::new(2, 1, 50, 50)))
            .unwrap();
        let buffer = terminal.backend().buffer();
        assert!(buffer[(3, 2)].modifier.contains(Modifier::DIM));
        assert!(!buffer[(0, 0)].modifier.contains(Modifier::DIM));
    }

    /// Paint one default cell and one explicit cell, then settle the ground.
    fn grounded(chrome: &Chrome) -> [(Color, Color); 2] {
        let mut terminal = Terminal::new(TestBackend::new(2, 1)).unwrap();
        terminal
            .draw(|frame| {
                frame.buffer_mut()[(1, 0)]
                    .set_fg(Color::Red)
                    .set_bg(Color::Blue);
                paint_ground(frame, chrome);
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        [
            (buffer[(0, 0)].fg, buffer[(0, 0)].bg),
            (buffer[(1, 0)].fg, buffer[(1, 0)].bg),
        ]
    }

    #[test]
    fn light_and_dark_paint_the_ground_system_leaves_the_terminals() {
        let mut chrome = Chrome::dark();
        let explicit = (Color::Red, Color::Blue);
        let dark = (chrome.palette.text, chrome.palette.panel_bg);
        assert_eq!(grounded(&chrome), [dark, explicit]);

        chrome.prefs.theme = "light".to_string();
        chrome.set_theme(ThemeKind::Light);
        let light = (chrome.palette.text, chrome.palette.panel_bg);
        assert_ne!(light, dark);
        assert_eq!(grounded(&chrome), [light, explicit]);

        chrome.prefs.theme = "System".to_string();
        assert_eq!(grounded(&chrome), [(Color::Reset, Color::Reset), explicit]);
    }

    /// Panes answer OSC 10/11 with the colours their default cells end up
    /// on, in every theme. Codex reads that answer once at startup and draws
    /// its composer bands from it, so a wrong one stays wrong (#23286).
    #[test]
    fn panes_answer_osc_10_11_with_the_ground_they_sit_on() {
        let rgb = |color: Option<RgbColor>| color.map(|c| Color::Rgb(c.r, c.g, c.b));
        let declared = |chrome: &Chrome| {
            let theme = chrome.terminal_theme();
            (rgb(theme.foreground), rgb(theme.background))
        };
        let painted = |chrome: &Chrome| {
            let [(fg, bg), _] = grounded(chrome);
            (Some(fg), Some(bg))
        };

        let mut chrome = Chrome::dark();
        assert_eq!(declared(&chrome), painted(&chrome), "dark");

        chrome.prefs.theme = "light".to_string();
        chrome.set_theme(ThemeKind::Light);
        assert_eq!(declared(&chrome), painted(&chrome), "light");

        chrome.prefs.monochrome = true;
        chrome.set_theme(ThemeKind::Light);
        assert_eq!(declared(&chrome), painted(&chrome), "monochrome light");
        chrome.prefs.monochrome = false;

        // System leaves default cells on the hosting terminal's own colours,
        // which gclient knows only from that terminal's answer: until then
        // panes answer nothing rather than the theme's guess.
        chrome.prefs.theme = "System".to_string();
        chrome.set_theme(ThemeKind::Dark);
        assert_eq!(painted(&chrome), (Some(Color::Reset), Some(Color::Reset)));
        assert_eq!(declared(&chrome), (None, None), "system before the answer");
        let host_bg = RgbColor {
            r: 0x1e,
            g: 0x1e,
            b: 0x2e,
        };
        let host_fg = RgbColor {
            r: 0xcd,
            g: 0xd6,
            b: 0xf4,
        };
        chrome.record_host_color(DefaultColorKind::Background, host_bg);
        chrome.record_host_color(DefaultColorKind::Foreground, host_fg);
        assert_eq!(
            declared(&chrome),
            (rgb(Some(host_fg)), rgb(Some(host_bg))),
            "system after the answer"
        );
    }
}
