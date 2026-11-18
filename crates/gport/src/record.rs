//! `gport record`: docs/port/record.md, its generator and its parser.

use std::io::Write;
use std::path::Path;

pub fn run(_root: &Path, _out: &mut dyn Write) -> anyhow::Result<bool> {
    anyhow::bail!("gport record is not implemented")
}

/// The paths under the `### Frozen files` heading of the frozen seam contract.
pub fn frozen_files(_record: &str) -> Vec<String> {
    Vec::new()
}

/// The Found Work lines under `chunk_id`.
pub fn found_work_lines(_record: &str, _chunk_id: &str) -> Vec<String> {
    Vec::new()
}
