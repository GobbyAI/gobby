//! Isolated supervisor-death interoperability peer; never a production entry point.
#[cfg(unix)]
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    use anyhow::{Context, ensure};
    use gobby_daemon::{
        lease::{ActiveDaemonLease, LeaseMode},
        lifecycle::{
            backend::{BackendConfig, BackendSupervisor},
            pid_file::{Role, claim_pid_file},
        },
    };
    use std::path::PathBuf;
    let home = PathBuf::from(std::env::var("GOBBY_LIFECYCLE_PEER_HOME")?);
    ensure!(home.is_dir(), "isolated home must exist");
    let url = std::env::var("DATABASE_URL")?;
    let parsed: tokio_postgres::Config = url.parse()?;
    ensure!(
        parsed.get_dbname() == Some("gobby_test")
            && parsed.get_ports() == [60892]
            && parsed.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "isolated peer only"
    );
    let mut lease = ActiveDaemonLease::new(&url, "process-peer", "lifecycle-test", LeaseMode::Hub)?;
    ensure!(lease.try_acquire().await?);
    let claim = claim_pid_file(&home.join("gobby.pid"), Role::Daemon)?.context("claim")?;
    let config = BackendConfig::from_environment(&home)?;
    let mut supervisor = BackendSupervisor::new(lease, claim, config);
    let (_tx, rx) = tokio::sync::watch::channel(false);
    supervisor.run(rx).await?;
    Ok(())
}

#[cfg(not(unix))]
fn main() {}
