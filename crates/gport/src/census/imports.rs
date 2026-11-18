//! Section 2: the modules outside each top-level package that import it.

use std::collections::{BTreeMap, BTreeSet};

use super::scan::TextFile;
use super::table;

const HEADING: &str = "## 2. In-process callers";

pub fn section(python: &[&TextFile], packages: &[String]) -> String {
    let mut callers: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for file in python {
        let inner = file.inner();
        let mut dirs: Vec<&str> = inner.split('/').collect();
        dirs.pop();
        let importer = dirs.first().copied().unwrap_or("");
        let mut module_parts = vec!["gobby"];
        module_parts.extend(dirs.iter().copied());
        for line in file.text.lines() {
            let statement = line.trim_start();
            if !statement.starts_with("import ") && !statement.starts_with("from ") {
                continue;
            }
            for package in imported_packages(statement, &module_parts) {
                if package != importer && packages.contains(&package) {
                    callers.entry(package).or_default().insert(inner.to_owned());
                }
            }
        }
    }
    let rows: Vec<Vec<String>> = packages
        .iter()
        .map(|package| {
            let listed: Vec<&str> = callers
                .get(package)
                .map_or_else(Vec::new, |set| set.iter().map(String::as_str).collect());
            let cell = if listed.is_empty() {
                "none".to_owned()
            } else {
                listed.join(", ")
            };
            vec![package.clone(), cell]
        })
        .collect();
    format!(
        "{HEADING}\n\nFor each top-level package, the modules under src/gobby outside it that \
         import it by absolute or relative import.\n\n{}",
        table(&["Package", "Outside callers"], &rows)
    )
}

/// The top-level packages one import statement brings in, for a module whose package path is
/// `module_parts` (starting with `gobby`). A `from` import of `gobby` itself names its packages.
fn imported_packages(statement: &str, module_parts: &[&str]) -> Vec<String> {
    let statement = statement.split('#').next().unwrap_or("").trim();
    if let Some(list) = statement.strip_prefix("import ") {
        return list
            .split(',')
            .filter_map(|item| {
                let dotted = item.split_whitespace().next()?;
                top_package(&dotted.split('.').collect::<Vec<_>>())
            })
            .collect();
    }
    let Some(rest) = statement.strip_prefix("from ") else {
        return Vec::new();
    };
    let Some((module, names)) = rest.split_once(" import ") else {
        return Vec::new();
    };
    let module = module.trim();
    let level = module.len() - module.trim_start_matches('.').len();
    let tail: Vec<&str> = module[level..]
        .split('.')
        .filter(|part| !part.is_empty())
        .collect();
    let target: Vec<&str> = if level == 0 {
        tail
    } else {
        let Some(keep) = module_parts.len().checked_sub(level - 1) else {
            return Vec::new();
        };
        module_parts[..keep].iter().copied().chain(tail).collect()
    };
    if let Some(package) = top_package(&target) {
        return vec![package];
    }
    if target.len() == 1 && target[0] == "gobby" {
        return names
            .split(',')
            .filter_map(|name| {
                let name = name.trim().trim_matches(|c| c == '(' || c == ')').trim();
                name.split_whitespace()
                    .next()
                    .filter(|imported| *imported != "*")
                    .map(str::to_owned)
            })
            .collect();
    }
    Vec::new()
}

/// The package named by a dotted path that starts with `gobby.`, such as `gobby.alpha.store`.
fn top_package(parts: &[&str]) -> Option<String> {
    match parts {
        ["gobby", package, ..] => Some((*package).to_owned()),
        _ => None,
    }
}
