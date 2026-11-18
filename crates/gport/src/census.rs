//! `gport census`: callers, writers, caches, registries and surfaces with no row.

use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default)]
pub struct Options {
    pub generations: Option<PathBuf>,
    pub no_python: bool,
}

pub fn run(_root: &Path, _options: Options, _out: &mut dyn Write) -> anyhow::Result<bool> {
    anyhow::bail!("gport census is not implemented")
}
