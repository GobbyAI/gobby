//! `gport ledger`: the parity ledger from the frozen rows, with ownership and dispositions.

use std::io::Write;
use std::path::Path;

#[derive(Debug, Clone, Copy, Default)]
pub struct Options {
    pub stdout: bool,
    pub exit_rule: bool,
    pub owners: bool,
}

pub fn run(_root: &Path, _options: Options, _out: &mut dyn Write) -> anyhow::Result<bool> {
    anyhow::bail!("gport ledger is not implemented")
}
