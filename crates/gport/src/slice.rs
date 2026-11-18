//! `gport check-slice`: one chunk's commits and paths, and the rate report.

use std::io::Write;
use std::path::Path;

pub fn check(
    _root: &Path,
    _chunk_id: &str,
    _tasks: &[String],
    _out: &mut dyn Write,
) -> anyhow::Result<bool> {
    anyhow::bail!("gport check-slice is not implemented")
}

pub fn report(_root: &Path, _chunk_ids: &[String], _out: &mut dyn Write) -> anyhow::Result<bool> {
    anyhow::bail!("gport check-slice --report is not implemented")
}
