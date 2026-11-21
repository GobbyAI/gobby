//! Jobs and terminal authority exist only while the native runtime is active.

#[cfg(test)]
#[path = "../tests/fixtures/host_process.rs"]
mod host_process_fixture;

use crate::family_start::{FAMILY_STOP_BUDGET, ProcessFamilies};
use crate::front_door::FrontDoorState;
use crate::heartbeat::HeartbeatHost;
use crate::lease::LeaseState;
use crate::lifecycle::backend::{LeaseScoped, SupervisorExit};
use anyhow::{Context, Result};
use gobby_core::bootstrap::HubDatabaseBootstrap;
use gobby_core::family::FamilyContext;
use gobby_core::family::lifecycle::{
    ActivationEntry, StopMode, activate_families, deactivate_families,
};
use gobby_core::postgres_pool::Pool;
use gobby_terminals::family::{RouteLatch, SupervisorMode};
use gobby_terminals::write_api::{Route, TokenSlot};
use std::{path::Path, sync::Arc};
use tokio::{sync::watch, task::JoinHandle, time::Instant};

pub(crate) struct ActiveServices {
    retention_stop: watch::Sender<bool>,
    retention: Option<JoinHandle<()>>,
    terminals: Option<gobby_terminals::family::TerminalFamily>,
    /// The lease's family jobs, staged at activation and scheduled from their admission
    /// ([`LeaseScoped::admit`]) until stop.
    jobs: HeartbeatHost,
    /// The lease watch that fences the jobs once they are admitted; None without a lease.
    job_fence: Option<watch::Receiver<LeaseState>>,
    /// The families this lease activated, deactivated at stop.
    activated: Vec<ActivationEntry<FrontDoorState>>,
    /// How the jobs ended, once [`ActiveServices::end_jobs`] ended them before the lease
    /// was released.
    jobs_ended: Option<Result<()>>,
}

impl ActiveServices {
    /// The write route, present while these services own the terminals.
    pub(crate) fn route(&self) -> Option<Arc<Route>> {
        self.terminals
            .as_ref()
            .and_then(gobby_terminals::family::TerminalFamily::route)
    }

    /// Starts the lease's services: the native pools at their share, the terminal host when
    /// this daemon owns the terminals, retention, and then the families' activation. The
    /// jobs that activation stages start only at their admission ([`LeaseScoped::admit`]),
    /// fenced by `lease` when the daemon holds one. A fatal activation stops what already
    /// started and fails the start.
    pub(crate) async fn start(
        bootstrap: &HubDatabaseBootstrap,
        terminals: &RouteLatch,
        home: &Path,
        token: TokenSlot,
        families: &ProcessFamilies,
        lease: Option<watch::Receiver<LeaseState>>,
    ) -> Result<Self> {
        let mode = terminals.select(crate::front_door::routes::terminal_route(
            &bootstrap.front_door.routes,
        ))?;
        let (retention_stop, retention_shutdown) = watch::channel(false);
        let mut services = Self {
            retention_stop,
            retention: None,
            terminals: None,
            jobs: HeartbeatHost::new(),
            job_fence: lease,
            activated: Vec::new(),
            jobs_ended: None,
        };
        if let Some(url) = &bootstrap.database_url {
            let machine = gobby_core::machine::read_machine_id_from_home(home)?;
            let pools = crate::native_pools::build_set(url, mode == SupervisorMode::Owner)?;
            if let (Some(writes), Some(authority)) =
                (pools.terminal_writes, pools.terminal_authority)
            {
                services.terminals = start_terminal_host(
                    terminals,
                    &pools.active,
                    home,
                    &machine,
                    (writes, authority),
                    token,
                    &families.ctx,
                )
                .await?;
            }
            services.retention = Some(tokio::spawn(crate::retention::run(
                pools.active,
                uuid::Uuid::parse_str(&machine)?,
                retention_shutdown,
            )));
        }
        match activate_families(
            &families.activation,
            &families.ctx,
            &mut services.jobs,
            FAMILY_STOP_BUDGET,
        )
        .await
        {
            Ok(report) => {
                for failure in report.ordinary {
                    tracing::warn!(
                        target: "gobby.daemon",
                        "family {} activated degraded: {}",
                        failure.family,
                        failure.message
                    );
                }
            }
            Err(failure) => {
                // The runner already unwound the families of this pass.
                services.stop(false).await?;
                anyhow::bail!(
                    "family {} failed to activate: {}",
                    failure.family,
                    failure.message
                );
            }
        }
        services.activated = families.activation.clone();
        Ok(services)
    }

    /// Ends the lease's family jobs before the supervisor releases the lease, so no job
    /// overlaps a new owner. A lost lease cannot be held through a drain, so its jobs are
    /// cancelled at once; every other end (`exit` is None for a supervisor error) drains
    /// them.
    pub(crate) async fn end_jobs(&mut self, exit: Option<SupervisorExit>) {
        let ended = match exit {
            Some(SupervisorExit::LeaseLost) => self.jobs.cancel().await,
            _ => self.jobs.stop().await,
        };
        self.jobs_ended = Some(ended);
    }

    /// Stops the lease's services in order: the jobs (unless [`ActiveServices::end_jobs`]
    /// already ended them), so none calls a deactivated family; the families, sharing one
    /// stop deadline; the terminal host, drained or left running; then retention. A drain
    /// fails on the first family that could not deactivate.
    pub(crate) async fn stop(mut self, drain: bool) -> Result<()> {
        let jobs = match self.jobs_ended.take() {
            Some(ended) => ended,
            None => self.jobs.stop().await,
        };
        let mode = if drain {
            StopMode::Drain
        } else {
            StopMode::Stop
        };
        let deadline = Instant::now() + FAMILY_STOP_BUDGET;
        let failures = deactivate_families(&self.activated, mode, deadline).await;
        for failure in &failures {
            tracing::warn!(
                target: "gobby.daemon",
                "family {} deactivate: {}",
                failure.family,
                failure.message
            );
        }
        let terminals = match self.terminals {
            Some(family) if drain => family
                .drain()
                .await
                .context("explicit terminal drain failed"),
            Some(family) => {
                family.stop().await;
                Ok(())
            }
            None => Ok(()),
        };
        // A failed drain still stops and joins retention before it is reported.
        self.retention_stop.send_replace(true);
        let retention = match self.retention {
            Some(task) => task.await.context("token-event retention task failed"),
            None => Ok(()),
        };
        terminals?;
        retention?;
        jobs.context("the lease's family jobs did not stop")?;
        if drain && let Some(failure) = failures.into_iter().next() {
            anyhow::bail!(
                "family {} did not drain: {}",
                failure.family,
                failure.message
            );
        }
        Ok(())
    }
}

impl LeaseScoped for ActiveServices {
    fn admit(&mut self) -> Result<()> {
        match self.job_fence.take() {
            Some(lease) => self.jobs.start_fenced(lease),
            None => self.jobs.start(),
        }
    }

    fn end(&mut self, exit: Option<SupervisorExit>) -> impl Future<Output = ()> + Send {
        self.end_jobs(exit)
    }
}

/// Starts the terminal host this daemon owns, on its write and authority pools. A host
/// whose start fails is kept (its supervisor retries) and marks the context degraded.
async fn start_terminal_host(
    terminals: &RouteLatch,
    active: &Pool,
    home: &Path,
    machine: &str,
    (writes, authority): (Pool, Pool),
    token: TokenSlot,
    ctx: &FamilyContext,
) -> Result<Option<gobby_terminals::family::TerminalFamily>> {
    let Some(options) =
        gobby_terminals::host::HostOptions::from_pool(active, home, machine.to_owned()).await?
    else {
        return Ok(None);
    };
    let family = gobby_terminals::family::TerminalFamily::new(
        terminals,
        options.clone(),
        Arc::new(gobby_terminals::host::PostgresEpochAuthority::new(
            authority,
        )),
    )
    .with_write_route(&options, Arc::new(writes), token);
    if let Err(error) = family.start().await {
        let line = report_terminal_host_failure(ctx, &error.to_string());
        tracing::warn!(target: "gobby.daemon", "{line}");
    }
    Ok(Some(family))
}

/// Marks the terminal host degraded after its start failed and returns the line that
/// reports the failure.
pub(crate) fn report_terminal_host_failure(ctx: &FamilyContext, error: &str) -> String {
    ctx.degraded().mark("gterm_host");
    format!("gdaemon terminal host: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_terminals::host::{EpochAuthority, EpochBinding, HostFailure, HostOptions};
    use std::{future::Future, pin::Pin, time::Duration};

    #[test]
    fn report_terminal_host_failure_marks_and_returns_line() {
        let ctx = FamilyContext::unconfigured();

        let line = report_terminal_host_failure(&ctx, "boom");

        assert_eq!(line, "gdaemon terminal host: boom");
        assert!(ctx.degraded().contains("gterm_host"));
    }

    struct EmptyAuthority;
    impl EpochAuthority for EmptyAuthority {
        fn bindings<'a>(
            &'a self,
            _: &'a str,
        ) -> Pin<
            Box<
                dyn Future<Output = std::result::Result<Vec<EpochBinding>, HostFailure>>
                    + Send
                    + 'a,
            >,
        > {
            Box::pin(async { Ok(Vec::new()) })
        }
    }

    #[tokio::test]
    async fn explicit_drain_stops_host_default_stop_preserves_it() -> Result<()> {
        use crate::lifecycle::{
            backend::{BackendConfig, BackendSupervisor},
            pid_file::{Role, claim_pid_file},
        };
        let fixture_home = tempfile::tempdir()?;
        let runtime = fixture_home.path().join("uv-environment");
        let cache = fixture_home.path().join("uv-cache");
        let environment = std::collections::BTreeMap::from([
            (
                "UV_PROJECT_ENVIRONMENT".into(),
                runtime.clone().into_os_string(),
            ),
            ("UV_CACHE_DIR".into(), cache.clone().into_os_string()),
        ]);
        assert!(runtime.starts_with(fixture_home.path()));
        assert!(cache.starts_with(fixture_home.path()));
        assert!(
            !runtime.exists(),
            "fixture must start with a cold uv environment"
        );
        assert!(!cache.exists(), "fixture must start with a cold uv cache");
        // The shutdown helper uses only stdlib; prepare its interpreter without
        // installing the application's dependencies or downloading Python.
        let prewarm = tokio::time::timeout(
            Duration::from_secs(30),
            tokio::process::Command::new("uv")
                .args(["venv", "--quiet", "--no-python-downloads"])
                .arg(&runtime)
                .envs(&environment)
                .kill_on_drop(true)
                .output(),
        )
        .await
        .context("temporary uv fixture prewarm exceeded 30s")??;
        anyhow::ensure!(
            prewarm.status.success(),
            "temporary uv fixture prewarm failed: {}",
            String::from_utf8_lossy(&prewarm.stderr)
        );
        assert!(
            runtime.join("bin/python").is_file(),
            "cold uv startup must finish inside the temp home before the supervisor deadline"
        );
        for drain in [false, true] {
            let home = tempfile::tempdir_in(fixture_home.path())?;
            // The host's installed process-identity contract requires basename gterm.
            let binary = home.path().join("gterm");
            super::host_process_fixture::install(&binary)?;
            #[cfg(target_os = "macos")]
            anyhow::ensure!(
                std::process::Command::new("/usr/bin/codesign")
                    .args(["--force", "--sign", "-"])
                    .arg(&binary)
                    .output()?
                    .status
                    .success(),
                "identity fixture executable must be signed before launch"
            );
            let mut host = tokio::process::Command::new(&binary)
                .arg("600")
                .kill_on_drop(true)
                .spawn()?;
            std::fs::write(
                home.path().join("gterm.pid"),
                host.id().context("host pid")?.to_string(),
            )?;
            let latch = RouteLatch::new(
                gobby_core::bootstrap::RouteBackend::Native,
                gobby_terminals::family::WsRoutes::Registered,
            )?;
            let family = gobby_terminals::family::TerminalFamily::new(
                &latch,
                HostOptions {
                    socket_dir: home.path().to_owned(),
                    shutdown_grace: Duration::from_millis(100),
                    ..HostOptions::default()
                },
                Arc::new(EmptyAuthority),
            );
            let (retention_stop, _) = watch::channel(false);
            let services = ActiveServices {
                retention_stop,
                retention: None,
                terminals: Some(family),
                jobs: HeartbeatHost::new(),
                job_fence: None,
                activated: Vec::new(),
                jobs_ended: None,
            };
            let script = format!(
                "import os,pathlib,json,time; p=pathlib.Path(os.environ['GOBBY_HOME']); (p/'shutdown_intent_active.json').write_text(json.dumps({{'intent':'stop','timestamp':time.time(),'details':{{'drain_terminals':{}}}}})); raise SystemExit(0)",
                if drain { "True" } else { "False" }
            );
            let config = BackendConfig {
                home: home.path().to_owned(),
                command: ["uv", "run", "--no-sync", "python", "-c", &script]
                    .into_iter()
                    .map(std::ffi::OsString::from)
                    .collect(),
                environment: environment.clone(),
                front_door_secret: None,
                drain_timeout: Duration::from_secs(17),
                heartbeat_interval: Duration::from_secs(1),
                write_token: gobby_terminals::write_api::TokenSlot::default(),
                exit_grace: Duration::from_secs(60),
                report_interval: Duration::from_secs(60),
                lease_lock_limit: Duration::from_secs(120),
                reporter: Arc::new(|line: &str| crate::logging::raw_stderr(line)),
            };
            for key in ["UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR"] {
                let path = std::path::PathBuf::from(
                    config
                        .environment
                        .get(std::ffi::OsStr::new(key))
                        .context(key)?,
                );
                assert!(
                    path.starts_with(fixture_home.path()),
                    "fixture {key} escaped temp home"
                );
            }
            let claim = claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?
                .context("test claim")?;
            let mut supervisor = BackendSupervisor::with_lease(None, claim, config);
            let (_stop, shutdown) = watch::channel(false);
            let outcome =
                tokio::time::timeout(Duration::from_secs(5), supervisor.run(shutdown)).await??;
            assert_eq!(outcome.drains_terminals(), drain);
            services.stop(outcome.drains_terminals()).await?;
            if drain {
                tokio::time::timeout(Duration::from_secs(2), host.wait()).await??;
            } else {
                assert!(
                    host.try_wait()?.is_none(),
                    "default stop killed the detached host"
                );
                host.kill().await?;
            }
        }
        Ok(())
    }
}
