//! herdr `src/ui/dialogs.rs` (4) keep-set render tests.

use gobby_client::ui::chrome::Chrome;
use gobby_client::ui::dialogs::{render_dialog, CloseTarget, Dialog};
use gobby_client::ui::widgets::centered_popup_rect;
use ratatui::layout::Rect;

use super::fixtures::{rect_rows, render};
use super::token_map::theme;

/// Frame the dialog tests render into.
const AREA: Rect = Rect {
    x: 0,
    y: 0,
    width: 100,
    height: 30,
};

/// gclient `CONFIRM_CLOSE_POPUP_WIDTH` / `CONFIRM_CLOSE_POPUP_HEIGHT` (private
/// to `ui/dialogs.rs`), used to locate the popup's inner rows.
const CONFIRM_CLOSE_POPUP: (u16, u16) = (64, 6);

/// herdr's confirm-close acts on the sidebar's selected workspace. gclient's
/// sidebar rows are roster terminals, so its close scope is the terminal.
const CLOSE_TARGET: CloseTarget = CloseTarget::Terminal;

/// herdr `confirm_close_overlay_text(app, runtimes) -> (title, detail)`.
///
/// gclient derives no dialog text from workspace state: `Dialog::ConfirmClose`
/// carries the selected row's name as a caller-supplied `title` and its pane
/// count, and the renderer writes `Close <noun>?` over `<title> — <n> panes`.
/// herdr's display-name precedence (custom name, then live runtime cwd, then
/// terminal cwd, then identity cwd) therefore maps onto the name passed
/// here, and both rows are read back from the rendered popup.
fn confirm_close_overlay_text(name: &str, panes: usize) -> (String, String) {
    let mut chrome = Chrome::new(theme());
    chrome.dialog = Some(Dialog::ConfirmClose {
        target: CLOSE_TARGET,
        title: name.to_string(),
        panes,
    });
    let terminal = render(AREA.width, AREA.height, |frame| {
        render_dialog(frame, AREA, &chrome)
    });
    let popup = centered_popup_rect(AREA, CONFIRM_CLOSE_POPUP.0, CONFIRM_CLOSE_POPUP.1)
        .expect("confirm-close popup fits the test area");
    let inner = Rect::new(
        popup.x + 1,
        popup.y + 1,
        popup.width.saturating_sub(2),
        popup.height.saturating_sub(2),
    );
    let rows = rect_rows(&terminal, inner);
    (rows[0].trim().to_string(), rows[1].trim().to_string())
}

parity_tests! {
    "src/ui/dialogs.rs" => {
        fn confirm_close_text_uses_live_workspace_cwd_label() {
            // herdr: no custom name, identity cwd `/projects/original`, and the
            // attached terminal's live cwd `/projects/current`; the live cwd's
            // basename names the workspace. herdr's `workspace` noun is
            // gclient's `terminal` (plan 3.1 keymap/noun mapping).
            let (title, detail) = confirm_close_overlay_text("current", 1);

            assert_eq!(title, "Close terminal?");
            assert_eq!(detail, "current — 1 pane");
        }

        fn confirm_close_text_prefers_live_runtime_cwd_over_stale_terminal_cwd() {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("current-thread runtime")
                .block_on(async {
                    // herdr spawns a `/bin/sh` runtime in `<tmp>/current` while
                    // the terminal record still says `<tmp>/original`; the
                    // runtime's cwd wins. gclient terminals run in the daemon,
                    // so there is no local runtime to spawn.
                    let (_, detail) = confirm_close_overlay_text("current", 1);

                    assert_eq!(detail, "current — 1 pane");
                });
        }

        fn confirm_close_text_uses_selected_custom_name_instead_of_active_workspace_cwd() {
            // herdr: workspace `active` is active, workspace `selected` is the
            // sidebar selection with terminal cwd `/projects/current`; the
            // selected workspace's custom name wins over both.
            let (_, detail) = confirm_close_overlay_text("selected", 1);

            assert_eq!(detail, "selected — 1 pane");
        }

        // TODO(#21908): herdr's `main` is the repo checkout of a worktree space
        // whose linked worktree `issue` is also open, so closing the parent
        // closes the group and the dialog reports the group's scope. gclient
        // carries no worktree spaces until that plan lands.
        #[deferred = "TODO(#21908): worktree groups do not exist in gclient yet"]
        fn confirm_close_text_reports_parent_group_scope() {
            let (title, detail) = confirm_close_overlay_text("main", 2);

            assert_eq!(title, "Close worktree group?");
            assert_eq!(detail, "main — 2 workspaces, 2 panes");
        }
    }
}
