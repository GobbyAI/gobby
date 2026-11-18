//! `gport start`: protocol step 3, checked against the daemon.

use std::io::Write;
use std::path::Path;

pub fn run(_root: &Path, _chunk_id: &str, _out: &mut dyn Write) -> anyhow::Result<bool> {
    anyhow::bail!("gport start is not implemented")
}
