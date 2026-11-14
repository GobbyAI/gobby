//! A real executable with a controlled lifetime for terminal-host identity tests.

use anyhow::{Context, Result, ensure};
use std::{fs, path::Path, process::Command};

pub fn install(binary: &Path) -> Result<()> {
    // A copied system utility can dispatch by its executable name (gterm ends
    // in rm), independently of argv[0]. Build a tiny native process instead.
    let source = binary.with_extension("c");
    fs::write(
        &source,
        "#include <unistd.h>\nint main(void) { for (;;) pause(); }\n",
    )?;
    let output = Command::new("cc")
        .arg(&source)
        .arg("-o")
        .arg(binary)
        .output()
        .context("compile terminal-host process fixture")?;
    ensure!(
        output.status.success(),
        "compile terminal-host process fixture: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    fs::remove_file(source)?;
    Ok(())
}
