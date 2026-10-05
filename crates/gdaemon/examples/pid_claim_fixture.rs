//! Isolated interoperability test peer. All state is under the supplied PID path.
#[cfg(unix)]
fn main() -> anyhow::Result<()> {
    use gobby_daemon::lifecycle::pid_file::{Role, claim_from_environment, claim_pid_file};
    use std::io::{BufRead, Write};
    let mut args = std::env::args_os().skip(1);
    let path = std::path::PathBuf::from(
        args.next()
            .ok_or_else(|| anyhow::anyhow!("PID path required"))?,
    );
    let maintenance = args.next().as_deref() == Some(std::ffi::OsStr::new("maintenance"));
    let mut claim = if maintenance {
        claim_pid_file(&path, Role::Maintenance)?
    } else {
        claim_from_environment(&path)?
    }
    .ok_or_else(|| anyhow::anyhow!("claim refused"))?;
    println!(
        "ready {} {}",
        std::process::id(),
        claim.fileno().expect("held claim")
    );
    std::io::stdout().flush()?;
    let mut line = String::new();
    std::io::stdin().lock().read_line(&mut line)?;
    claim.release();
    Ok(())
}

#[cfg(not(unix))]
fn main() {
    eprintln!("Unix flock required");
    std::process::exit(1);
}
