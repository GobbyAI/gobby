//! `gport census`: package sizes, in-process callers, writers by table, caches, in-memory
//! registries, the tool-name sweep, surfaces with no row, and retained generations.
//!
//! Read-only. The report is Markdown on `out`, with eight `##` sections in that order. Each
//! section is built by its own module, so this file only reads the trees and orders the output.

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
use scan::TextFile;

#[derive(Debug, Clone, Default)]
pub struct Options {
    pub generations: Option<PathBuf>,
    pub no_python: bool,
}

pub fn run(root: &Path, options: Options, out: &mut dyn Write) -> anyhow::Result<bool> {
    if !root.join("src/gobby").is_dir() {
        bail!("no src/gobby directory under {}", root.display());
    }
    let src = scan::text_files(root, "src")?;
    let tests = scan::text_files(root, "tests")?;
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
    Ok(true)
}

/// A Markdown table: a header row, a divider, and one line per row. Cells are printed as given.
fn table(header: &[&str], rows: &[Vec<String>]) -> String {
    let divider = vec!["---"; header.len()];
    let mut text = format!("| {} |\n| {} |\n", header.join(" | "), divider.join(" | "));
    for row in rows {
        text.push_str(&format!("| {} |\n", row.join(" | ")));
    }
    text
}
