use std::collections::HashSet;

use crate::index::import_resolution::ImportResolutionContext;

use super::{ImportIdentity, merge_visible_imports};

pub(crate) fn identity_from(visible: &[&str], rows: &[(&str, &str)]) -> ImportIdentity {
    let visible = visible
        .iter()
        .map(|path| (*path).to_string())
        .collect::<HashSet<_>>();
    let imports = rows
        .iter()
        .map(|(source, module)| ((*source).to_string(), (*module).to_string()))
        .collect::<Vec<_>>();
    ImportIdentity::from_resolution(&visible, &ImportResolutionContext::default(), &imports)
}

fn rows(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(source, module)| ((*source).to_string(), (*module).to_string()))
        .collect()
}

#[test]
fn mcg_identity_build_handles_twenty_thousand_rows() {
    const FILES: usize = 2_000;
    let visible = (0..FILES)
        .map(|index| format!("src/m{index}.py"))
        .collect::<HashSet<_>>();
    let mut imports = Vec::with_capacity(FILES * 10);
    for index in 0..FILES {
        for offset in 1..=10 {
            imports.push((
                format!("src/m{index}.py"),
                format!("m{}", (index + offset) % FILES),
            ));
        }
    }
    let started = std::time::Instant::now();
    let identity =
        ImportIdentity::from_resolution(&visible, &ImportResolutionContext::default(), &imports);
    let elapsed = started.elapsed();
    assert_eq!(identity.aliases["src/m0.py"], vec!["m0", "src.m0"]);
    assert_eq!(identity.providers["m1999"], vec!["src/m1999.py"]);
    assert_eq!(identity.aliases.len(), FILES);
    assert!(
        elapsed < std::time::Duration::from_secs(30),
        "identity build took {elapsed:?}; the one-pass build must stay linear"
    );
}

/// An overlay context must see the parent's imports for every file the overlay
/// does not itself index, and must not see the parent's imports for a file the
/// overlay replaced — the rule `visibility::visible_tree` applies to the files.
#[test]
fn overlay_partition_covers_parent_only_files() {
    let overlay_paths = ["src/a.py".to_string(), "src/gone.py".to_string()]
        .into_iter()
        .collect::<HashSet<_>>();

    let merged = merge_visible_imports(
        rows(&[("src/a.py", "shared")]),
        rows(&[
            ("src/a.py", "stale"),
            ("src/b.py", "shared"),
            ("src/gone.py", "stale"),
            ("src/c.py", "b"),
        ]),
        &overlay_paths,
    );

    assert_eq!(
        merged,
        rows(&[
            ("src/a.py", "shared"),
            ("src/b.py", "shared"),
            ("src/c.py", "b"),
        ])
    );
}
