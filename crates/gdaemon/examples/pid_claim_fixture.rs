//! Isolated interoperability test peer. All state is under the supplied PID path.
#[cfg(unix)]
fn main() -> anyhow::Result<()> {
    use gobby_daemon::lifecycle::pid_file::{
        Role, claim_from_environment, claim_pid_file, decode_record,
    };
    use std::io::{BufRead, Write};
    let mut args = std::env::args_os().skip(1);
    let path = std::path::PathBuf::from(
        args.next()
            .ok_or_else(|| anyhow::anyhow!("PID path required"))?,
    );
    let mode = args.next();
    if mode.as_deref() == Some(std::ffi::OsStr::new("read-record")) {
        let record = decode_record(&std::fs::read(&path)?)
            .ok_or_else(|| anyhow::anyhow!("invalid PID record"))?;
        let pid: u32 = serde_json::from_str(
            record
                .get("pid")
                .ok_or_else(|| anyhow::anyhow!("missing PID"))?
                .get(),
        )?;
        let generation = record
            .get("generation")
            .ok_or_else(|| anyhow::anyhow!("missing generation"))?;
        println!("pid {pid} generation {}", generation.get());
        return Ok(());
    }
    let maintenance = mode.as_deref() == Some(std::ffi::OsStr::new("maintenance"));
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
