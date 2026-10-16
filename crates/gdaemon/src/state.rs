//! Single owner of the machine PID claim and runtime startup.
//!
//! Standalone and hub runtimes take a lease and backend lifecycle. A node takes
//! only the PID claim and NodeServices, without a native runtime or database.

use crate::{
    front_door::relay::HubRelay,
    lease::{ActiveDaemonLease, LeaseState},
    lifecycle::{
        backend::{BackendConfig, BackendStatus, BackendSupervisor, SupervisorExit},
        pid_file::{PidFileClaim, claim_from_environment},
    },
};
use anyhow::{Context, Result};
use gobby_core::bootstrap::{HubDatabaseBootstrap, RouteBackend, RunMode};
use gobby_core::postgres_pool::Pool;
use std::{
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::sync::{Mutex, watch};

/// Enrollment and hub destination for a node's front door.
pub struct NodeServices {
    pub bootstrap_path: PathBuf,
    pub relay: HubRelay,
}

impl NodeServices {
    pub fn prepare(
        bootstrap: &HubDatabaseBootstrap,
        path: &Path,
        home: &Path,
    ) -> Result<Option<(Arc<Self>, PidFileClaim)>> {
        bootstrap.api_key.as_ref().with_context(|| {
            format!(
                "node bootstrap {} has no api_key; run `gobby auth login` to enroll",
                path.display()
            )
        })?;
        let hub_daemon_url = bootstrap
            .hub_daemon_url
            .as_ref()
            .context("node bootstrap requires hub_daemon_url")?;
        let pem = if hub_daemon_url.starts_with("https://") {
            let cert = bootstrap.hub_cert.as_ref().with_context(|| {
                format!(
                    "node bootstrap {} has no hub_cert for an https hub; \
                     run `gobby auth login` again",
                    path.display()
                )
            })?;
            Some(
                std::fs::read(cert)
                    .with_context(|| format!("cannot read pinned hub certificate {cert}"))?,
            )
        } else {
            None
        };
        let relay = HubRelay::new(hub_daemon_url, pem.as_deref())?;
        let Some(claim) = claim_from_environment(&home.join("gobby.pid"))? else {
            return Ok(None);
        };
        Ok(Some((
            Arc::new(Self {
                bootstrap_path: path.to_owned(),
                relay,
            }),
            claim,
        )))
    }
}

/// The HTTP layer sees a projection, while the supervisor owns the PID claim.
pub struct RuntimeState {
    pub mode: RunMode,
    pub lease: Option<Arc<Mutex<ActiveDaemonLease>>>,
    pub lease_state: Option<watch::Receiver<LeaseState>>,
    pub backend: watch::Receiver<BackendStatus>,
}

impl RuntimeState {
    pub fn prepare(
        bootstrap: &HubDatabaseBootstrap,
        home: &Path,
        secret: &str,
        pool: Option<Arc<Pool>>,
    ) -> Result<Option<(Arc<Self>, BackendSupervisor)>> {
        if bootstrap.mode == RunMode::Node {
            anyhow::bail!(
                "a node takes no native runtime; NodeServices::prepare claims its PID file"
            );
        }
        let Some(claim) = claim_from_environment(&home.join("gobby.pid"))? else {
            return Ok(None);
        };
        let pool = pool.context("native runtime ownership requires bootstrap database_url")?;
        let machine = gobby_core::machine::read_machine_id_from_home(home)?;
        let token = gobby_core::grant::deployment_token(home);
        let lease = ActiveDaemonLease::new(pool, &machine, &token, bootstrap.mode)?;
        let lease_state = Some(lease.subscribe());
        let lease = Some(Arc::new(Mutex::new(lease)));
        let mut config = BackendConfig::from_environment(home)?;
        config
            .environment
            .insert("GOBBY_FRONT_DOOR_SECRET".into(), secret.into());
        // The route serve.rs latched from this bootstrap; the backend refuses a
        // bootstrap that names another route as restart-required.
        let route = match crate::front_door::routes::terminal_route(&bootstrap.front_door.routes) {
            RouteBackend::Proxy => "proxy",
            RouteBackend::Native => "native",
            RouteBackend::Compare => "compare",
        };
        config
            .environment
            .insert("GOBBY_TERMINAL_ROUTE".into(), route.into());
        let supervisor = BackendSupervisor::with_lease(lease.clone(), claim, config);
        let state = Arc::new(Self {
            mode: bootstrap.mode,
            lease,
            lease_state,
            backend: supervisor.subscribe(),
        });
        Ok(Some((state, supervisor)))
    }

    pub async fn run(
        &self,
        mut supervisor: BackendSupervisor,
        mut shutdown: watch::Receiver<bool>,
        bootstrap: HubDatabaseBootstrap,
        terminals: gobby_terminals::family::RouteLatch,
        home: std::path::PathBuf,
    ) -> Result<()> {
        loop {
            if *shutdown.borrow() {
                return Ok(());
            }
            if let Some(lease) = &self.lease {
                let acquired = match lease.lock().await.try_acquire().await {
                    Ok(acquired) => acquired,
                    Err(error) => {
                        eprintln!("gdaemon lease acquisition: {error:#}");
                        false
                    }
                };
                if !acquired {
                    tokio::select! {
                        changed = shutdown.changed() => {
                            if changed.is_err() {
                                return Ok(());
                            }
                        }
                        _ = tokio::time::sleep(Duration::from_secs(1)) => {}
                    }
                    continue;
                }
            }
            let services =
                crate::state_services::ActiveServices::start(&bootstrap, &terminals, &home).await?;
            let outcome = supervisor.run(shutdown.clone()).await;
            let drain = outcome
                .as_ref()
                .is_ok_and(|outcome| outcome.drains_terminals());
            services.stop(drain).await?;
            match outcome? {
                SupervisorExit::Stopped | SupervisorExit::StopTerminals => return Ok(()),
                SupervisorExit::Refused => {
                    // Refusal is sticky and keeps the machine claim, but releases
                    // database ownership. Only a full process restart retries.
                    while !*shutdown.borrow() {
                        if shutdown.changed().await.is_err() {
                            break;
                        }
                    }
                    return Ok(());
                }
                SupervisorExit::Standby
                | SupervisorExit::LeaseLost
                | SupervisorExit::RestartTerminals => {}
            }
        }
    }

    pub fn standby(&self) -> bool {
        if matches!(*self.backend.borrow(), BackendStatus::Refused { .. }) {
            return false;
        }
        match &self.lease_state {
            Some(state) => *state.borrow() != LeaseState::Active,
            None => false,
        }
    }
}
