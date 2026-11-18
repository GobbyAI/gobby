//! `gport shapes`: per-tool call counts and argument-key sets, with no values.

use std::io::Write;
use std::path::PathBuf;

#[derive(Debug, Clone, Default)]
pub struct Options {
    pub transcripts: Vec<PathBuf>,
    pub no_daemon: bool,
}

pub fn run(_options: Options, _out: &mut dyn Write) -> anyhow::Result<bool> {
    anyhow::bail!("gport shapes is not implemented")
}
