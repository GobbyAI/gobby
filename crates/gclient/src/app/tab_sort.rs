//! Tab title order for "Sort tabs A–Z": case-insensitive, with digit runs
//! compared by value so "Lane 2" sorts before "Lane 10".

use std::cmp::Ordering;
use std::iter::Peekable;
use std::str::Chars;

/// Orders two tab titles; an untitled (missing or blank) tab sorts last.
pub fn title_cmp(a: Option<&str>, b: Option<&str>) -> Ordering {
    fn named(title: Option<&str>) -> Option<&str> {
        title.map(str::trim).filter(|t| !t.is_empty())
    }
    match (named(a), named(b)) {
        (Some(a), Some(b)) => natural_cmp(a, b),
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => Ordering::Equal,
    }
}

/// Case-insensitive comparison that reads each run of ASCII digits as a number.
pub fn natural_cmp(a: &str, b: &str) -> Ordering {
    let (mut a, mut b) = (a.chars().peekable(), b.chars().peekable());
    loop {
        let ord = match (a.peek().copied(), b.peek().copied()) {
            (None, None) => return Ordering::Equal,
            (None, Some(_)) => return Ordering::Less,
            (Some(_), None) => return Ordering::Greater,
            (Some(x), Some(y)) if x.is_ascii_digit() && y.is_ascii_digit() => {
                let (x, y) = (digit_run(&mut a), digit_run(&mut b));
                let (x, y) = (x.trim_start_matches('0'), y.trim_start_matches('0'));
                x.len().cmp(&y.len()).then_with(|| x.cmp(y))
            }
            (Some(x), Some(y)) => {
                a.next();
                b.next();
                x.to_lowercase().cmp(y.to_lowercase())
            }
        };
        if ord != Ordering::Equal {
            return ord;
        }
    }
}

fn digit_run(chars: &mut Peekable<Chars<'_>>) -> String {
    let mut run = String::new();
    while let Some(digit) = chars.next_if(char::is_ascii_digit) {
        run.push(digit);
    }
    run
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::workspace_ops::WorkspaceModel;
    use crate::daemon::{TabRow, WorkspaceSnapshot};

    fn sorted<'a>(titles: &[&'a str]) -> Vec<&'a str> {
        let mut titles = titles.to_vec();
        titles.sort_by(|a, b| natural_cmp(a, b));
        titles
    }

    #[test]
    fn numbers_compare_by_value() {
        assert_eq!(
            sorted(&["Lane 10", "Lane 2", "Lane 1"]),
            ["Lane 1", "Lane 2", "Lane 10"]
        );
        assert_eq!(
            sorted(&["v1.10", "v1.9", "v1.09a"]),
            ["v1.9", "v1.09a", "v1.10"]
        );
    }

    #[test]
    fn case_is_ignored_and_prefixes_sort_first() {
        assert_eq!(
            sorted(&["beta", "Alpha", "alphabet"]),
            ["Alpha", "alphabet", "beta"]
        );
        assert_eq!(natural_cmp("LANE", "lane"), Ordering::Equal);
        assert_eq!(natural_cmp("007", "7"), Ordering::Equal);
    }

    #[test]
    fn untitled_tabs_sort_last() {
        assert_eq!(title_cmp(Some("zed"), None), Ordering::Less);
        assert_eq!(title_cmp(Some("  "), Some("zed")), Ordering::Greater);
        assert_eq!(title_cmp(None, Some("")), Ordering::Equal);
        assert_eq!(title_cmp(Some(" b"), Some("a ")), Ordering::Greater);
    }

    fn model(tabs: &[(&str, Option<&str>, u32)]) -> WorkspaceModel {
        let mut snapshot: WorkspaceSnapshot = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/terminal_ws_golden/workspace_snapshot.json"
        ))
        .unwrap();
        let template = snapshot.tabs[0].clone();
        snapshot.tabs = tabs
            .iter()
            .enumerate()
            .map(|(reference, &(id, title, position))| TabRow {
                id: id.to_owned(),
                reference: reference as u64,
                title: title.map(str::to_owned),
                position,
                ..template.clone()
            })
            .collect();
        snapshot.panes.clear();
        WorkspaceModel::from_snapshot(snapshot)
    }

    #[test]
    fn sorted_tab_order_names_every_tab_in_title_order() {
        let model = model(&[
            ("t-untitled", None, 0),
            ("t-lane-10", Some("Lane 10"), 1),
            ("t-blank", Some(" "), 2),
            ("t-lane-2", Some("lane 2"), 3),
            ("t-alpha", Some("Alpha"), 4),
        ]);
        assert_eq!(
            model.sorted_tab_order().unwrap(),
            ["t-alpha", "t-lane-2", "t-lane-10", "t-untitled", "t-blank"]
        );
    }

    #[test]
    fn sorted_tab_order_is_none_when_already_sorted() {
        let model = model(&[
            ("t-b", Some("Beta"), 1),
            ("t-a", Some("Alpha"), 0),
            ("t-none", None, 2),
        ]);
        assert_eq!(model.sorted_tab_order(), None);
    }
}
