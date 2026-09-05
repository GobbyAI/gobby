//! herdr keep-set render-test parity harness (plan 4.1).
//!
//! Every module below ports the render tests of one or more herdr source files
//! at the pinned fork commit with their row-text expectations unchanged.
//! `upstream_tests.txt` is the completeness oracle: a deterministic extraction
//! of the pinned tree whose digest is asserted before any comparison, so the
//! inventory, the ported set, and the `UPSTREAM.md` parity table cannot agree
//! with each other while disagreeing with upstream.

use std::collections::{BTreeMap, BTreeSet};

use sha2::{Digest, Sha256};

/// Registers ported herdr tests.
///
/// Each block names the herdr source path its tests came from. Every `fn`
/// becomes a plain `#[test]` (so nextest lists it individually) and a
/// `CASES` entry keyed by `<source_path>::<test_name>`, which the harness
/// compares against `upstream_tests.txt`. Herdr `#[tokio::test]` cases are
/// ported as sync fns that build a runtime inside the body.
macro_rules! parity_tests {
    ($( $path:literal => { $( fn $name:ident () $body:block )* } )*) => {
        $( $( #[test] fn $name() $body )* )*

        /// `(herdr identity, test)` pairs registered by this module.
        pub const CASES: &[(&str, fn())] = &[
            $( $( (concat!($path, "::", stringify!($name)), $name), )* )*
        ];
    };
}

mod fixtures;
mod token_map;

mod chrome;
mod dialogs;
mod navigator;
mod panes;
mod sidebar;
mod status;
mod tabs;

/// herdr commit the keep-set was extracted from (fork point, see UPSTREAM.md).
const UPSTREAM_COMMIT: &str = "346411fa21afd297f5ed3b3fa56f9e3fbf7654b7";
const INVENTORY: &str = include_str!("upstream_tests.txt");
const INVENTORY_LINES: usize = 113;
const INVENTORY_SHA256: &str = "6d3cb09874a9c2b47a0b6982b4a1ccb920c77e435933bfada7e26d9ef116d412";
const UPSTREAM_MD: &str = include_str!("../../UPSTREAM.md");
const PARITY_TABLE_START: &str = "<!-- parity-table:start -->";
const PARITY_TABLE_END: &str = "<!-- parity-table:end -->";

type Cases = &'static [(&'static str, fn())];

fn modules() -> [(&'static str, Cases); 7] {
    [
        ("chrome", chrome::CASES),
        ("dialogs", dialogs::CASES),
        ("navigator", navigator::CASES),
        ("panes", panes::CASES),
        ("sidebar", sidebar::CASES),
        ("status", status::CASES),
        ("tabs", tabs::CASES),
    ]
}

fn ported_identities() -> Vec<&'static str> {
    modules()
        .iter()
        .flat_map(|(_, cases)| cases.iter().map(|(id, _)| *id))
        .collect()
}

/// Per-source-path ported counts recorded in `UPSTREAM.md`'s parity table.
///
/// The table sits between the two HTML comment markers; each row is
/// `` | `src/...` | <ported> | <not ported> | <gclient module> | ``.
fn upstream_md_counts() -> BTreeMap<String, usize> {
    let start = UPSTREAM_MD
        .find(PARITY_TABLE_START)
        .expect("UPSTREAM.md has a parity-table:start marker");
    let end = UPSTREAM_MD
        .find(PARITY_TABLE_END)
        .expect("UPSTREAM.md has a parity-table:end marker");
    assert!(start < end, "parity table markers are out of order");
    let mut counts = BTreeMap::new();
    for line in UPSTREAM_MD[start..end].lines() {
        let cells: Vec<&str> = line.split('|').map(str::trim).collect();
        // A table row splits into an empty lead, the cells, and an empty tail.
        if cells.len() < 4 {
            continue;
        }
        let Some(path) = cells[1].strip_prefix('`').and_then(|c| c.strip_suffix('`')) else {
            continue;
        };
        let ported: usize = cells[2]
            .parse()
            .unwrap_or_else(|_| panic!("parity table row {line:?} has a non-numeric ported count"));
        let previous = counts.insert(path.to_string(), ported);
        assert!(previous.is_none(), "parity table lists {path} twice");
    }
    counts
}

/// 4.1.2: the committed inventory is the pinned upstream extraction, the
/// ported identity set equals it exactly, and `UPSTREAM.md`'s per-module
/// counts equal the ported set.
#[test]
fn ported_set_matches_upstream_inventory() {
    let digest = format!("{:x}", Sha256::digest(INVENTORY.as_bytes()));
    assert_eq!(
        digest, INVENTORY_SHA256,
        "upstream_tests.txt is not the pinned extraction of herdr {UPSTREAM_COMMIT}"
    );
    assert!(INVENTORY.ends_with('\n'), "inventory is newline-terminated");
    let lines: Vec<&str> = INVENTORY.lines().collect();
    assert_eq!(lines.len(), INVENTORY_LINES);
    let mut sorted = lines.clone();
    sorted.sort_unstable();
    assert_eq!(sorted, lines, "inventory is sorted ascending");
    let expected: BTreeSet<&str> = lines.iter().copied().collect();
    assert_eq!(
        expected.len(),
        INVENTORY_LINES,
        "inventory has no duplicates"
    );

    let ported = ported_identities();
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    let duplicates: Vec<&str> = ported
        .iter()
        .copied()
        .filter(|id| !seen.insert(id))
        .collect();
    assert!(duplicates.is_empty(), "ported twice: {duplicates:#?}");
    let missing: Vec<&&str> = expected.difference(&seen).collect();
    let extra: Vec<&&str> = seen.difference(&expected).collect();
    assert!(
        missing.is_empty() && extra.is_empty(),
        "ported set differs from upstream_tests.txt\nmissing: {missing:#?}\nextra: {extra:#?}"
    );

    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for id in &ported {
        let (path, _) = id.split_once("::").expect("identity is <path>::<name>");
        *counts.entry(path.to_string()).or_default() += 1;
    }
    let mut recorded = upstream_md_counts();
    recorded.retain(|_, ported| *ported > 0);
    assert_eq!(
        recorded, counts,
        "UPSTREAM.md parity table disagrees with the ported set"
    );
}

/// 4.1.1: every ported keep-set test passes, reported as one list so a
/// regression names every affected identity at once.
#[test]
fn keep_set_render_tests_all_pass() {
    let previous_hook = std::panic::take_hook();
    // Failing cases are collected below; the default hook's per-panic
    // backtrace chatter would bury the list.
    std::panic::set_hook(Box::new(|_| {}));
    let mut failures = Vec::new();
    for (_, cases) in modules() {
        for (id, case) in cases {
            if std::panic::catch_unwind(case).is_err() {
                failures.push(*id);
            }
        }
    }
    std::panic::set_hook(previous_hook);
    assert!(
        failures.is_empty(),
        "{} of {} keep-set tests fail: {failures:#?}",
        failures.len(),
        INVENTORY_LINES
    );
}
