//! `gport census`: package sizes, in-process callers, writers by table, caches, in-memory
//! registries, the tool-name sweep, surfaces with no row, and retained generations.
//!
//! Read-only. The report is Markdown on `out`: eight numbered `##` sections in that order, then a
//! `## Skipped files` list of the files the census could not read or decode. Each section is built
//! by its own module, so this file only reads the trees and orders the output.

mod caches;
mod generations;
mod imports;
mod registries;
mod scan;
mod sizes;
mod surfaces;
mod sweep;
mod tables;

use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::bail;

use crate::FROZEN_LEDGER_PATH;
use scan::{Skipped, TextFile};

#[derive(Debug, Clone, Default)]
pub struct Options {
    pub generations: Option<PathBuf>,
    pub no_python: bool,
}

pub fn run(root: &Path, options: Options, out: &mut dyn Write) -> anyhow::Result<bool> {
    if !root.join("src/gobby").is_dir() {
        bail!("no src/gobby directory under {}", root.display());
    }
    let mut skipped = Vec::new();
    let src = scan::text_files(root, "src", &mut skipped);
    let tests = scan::text_files(root, "tests", &mut skipped);
    let python: Vec<&TextFile> = src.iter().filter(|file| file.is_gobby_python()).collect();
    let packages = scan::packages(&src);
    let frozen_text = std::fs::read_to_string(root.join(FROZEN_LEDGER_PATH))
        .map_err(|error| format!("cannot read {FROZEN_LEDGER_PATH}: {error}"));
    let frozen = frozen_text.as_deref().map_err(|reason| reason.as_str());

    write!(out, "# gport census\n\n")?;
    write!(out, "{}", sizes::section(&src, &tests, &packages))?;
    write!(out, "{}", imports::section(&python, &packages))?;
    write!(out, "{}", tables::section(&python)?)?;
    write!(out, "{}", caches::section(&python))?;
    write!(out, "{}", registries::section(&python))?;
    write!(out, "{}", sweep::section(&python, frozen))?;
    write!(
        out,
        "{}",
        surfaces::section(root, frozen, options.no_python)
    )?;
    write!(
        out,
        "{}",
        generations::section(options.generations.as_deref())
    )?;
    write!(out, "{}", skipped_section(&mut skipped))?;
    Ok(true)
}

/// The files the census did not read, sorted by path, with their count and reasons. A skipped file
/// is left out of every section above.
fn skipped_section(skipped: &mut [Skipped]) -> String {
    skipped.sort_by(|a, b| a.path.cmp(&b.path));
    if skipped.is_empty() {
        return "## Skipped files\n\nNo files were skipped.\n".to_owned();
    }
    let rows: Vec<Vec<String>> = skipped
        .iter()
        .map(|entry| vec![entry.path.clone(), entry.reason.clone()])
        .collect();
    format!(
        "## Skipped files\n\n{} file(s) skipped. Each one is left out of the sections above.\n\n{}",
        skipped.len(),
        table(&["Path", "Reason"], &rows)
    )
}

/// A table cell on one line, with pipes escaped. The same rule as the `record` tables.
fn cell(value: &str) -> String {
    value.replace(['\r', '\n'], " ").replace('|', "\\|")
}

/// A Markdown table: a header row, a divider, and one line per row. Every header and body cell
/// goes through `cell`, so a pipe or a line break in a path or a name cannot split a row.
fn table(header: &[&str], rows: &[Vec<String>]) -> String {
    let divider = vec!["---"; header.len()];
    let head: Vec<String> = header.iter().map(|name| cell(name)).collect();
    let mut text = format!("| {} |\n| {} |\n", head.join(" | "), divider.join(" | "));
    for row in rows {
        let cells: Vec<String> = row.iter().map(|value| cell(value)).collect();
        text.push_str(&format!("| {} |\n", cells.join(" | ")));
    }
    text
}

#[cfg(test)]
mod tests {
    use super::scan::Skipped;
    use super::skipped_section;

    #[test]
    fn skipped_section_escapes_pipes_and_line_breaks_in_both_cells() {
        let mut skipped = vec![Skipped {
            path: "src/gobby/a|b\nc.py".to_owned(),
            reason: "cannot read: x|y\nz".to_owned(),
        }];
        let text = skipped_section(&mut skipped);
        assert!(
            text.lines()
                .any(|line| line == "| src/gobby/a\\|b c.py | cannot read: x\\|y z |"),
            "pipes should be escaped and line breaks replaced in both cells:\n{text}"
        );
    }
}
