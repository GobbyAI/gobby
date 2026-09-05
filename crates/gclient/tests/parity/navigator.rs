//! herdr `src/ui/navigator.rs` (5) keep-set render tests.

use gobby_client::ui::chrome::RowState;
use gobby_client::ui::navigator::{
    has_following_sibling_at_depth, tree_prefix, NavigatorRow, NavigatorTarget,
};

/// herdr's `row(depth, is_workspace)` fixture. gclient's navigator has no
/// workspace targets, labels, or seen/current/tab/match flags, so the row
/// carries an inert terminal target and an idle roster state; only `depth`,
/// `is_workspace`, and `expanded` feed the tree-prefix arithmetic.
fn row(depth: u8, is_workspace: bool) -> NavigatorRow {
    NavigatorRow {
        target: NavigatorTarget::Terminal(String::new()),
        title: String::new(),
        state: RowState::Idle,
        detail: String::new(),
        depth,
        is_workspace,
        expanded: true,
    }
}

fn multi_tab_rows() -> Vec<NavigatorRow> {
    vec![
        row(0, true),  // workspace
        row(1, false), // tab a
        row(2, false), // pane
        row(2, false), // pane (last in tab a)
        row(1, false), // tab b (last tab)
        row(2, false), // pane (last in tab b)
        row(0, true),  // workspace
        row(1, false), // pane (single child)
    ]
}

parity_tests! {
    "src/ui/navigator.rs" => {
        fn workspace_rows_use_expand_caret() {
            let rows = multi_tab_rows();
            assert_eq!(tree_prefix(&rows, 0), "▾");
            let mut collapsed = rows.clone();
            collapsed[0].expanded = false;
            assert_eq!(tree_prefix(&collapsed, 0), "▸");
        }

        fn middle_children_get_branch_glyph() {
            let rows = multi_tab_rows();
            assert_eq!(tree_prefix(&rows, 1), "├──");
            assert_eq!(tree_prefix(&rows, 2), "│  ├──");
        }

        fn last_children_get_terminator_glyph() {
            let rows = multi_tab_rows();
            assert_eq!(tree_prefix(&rows, 3), "│  └──");
            assert_eq!(tree_prefix(&rows, 4), "└──");
            assert_eq!(tree_prefix(&rows, 7), "└──");
        }

        fn spine_stops_after_last_ancestor_sibling() {
            let rows = multi_tab_rows();
            assert_eq!(tree_prefix(&rows, 5), "   └──");
        }

        fn next_workspace_does_not_extend_previous_subtree() {
            // The pane at idx 5 is last in its workspace even though another
            // workspace with children follows.
            let rows = multi_tab_rows();
            assert!(!has_following_sibling_at_depth(&rows, 5, 1));
            assert!(!has_following_sibling_at_depth(&rows, 5, 2));
        }
    }
}
