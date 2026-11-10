//! Jobs and terminal authority exist only while the native runtime is active.

use anyhow::{Context, Result};
use gobby_core::{
    bootstrap::HubDatabaseBootstrap,
    postgres_pool::{Pool, PoolSettings},
};
use gobby_terminals::family::{RouteLatch, SupervisorMode};
use gobby_terminals::write_api::{Route, TokenSlot};
use std::{path::Path, sync::Arc, time::Duration};
use tokio::{sync::watch, task::JoinHandle};

pub(crate) struct ActiveServices {
    retention_stop: watch::Sender<bool>,
    retention: Option<JoinHandle<()>>,
    terminals: Option<gobby_terminals::family::TerminalFamily>,
}

impl ActiveServices {
    /// The write route, present while these services own the terminals.
    pub(crate) fn route(&self) -> Option<Arc<Route>> {
        self.terminals
            .as_ref()
            .and_then(gobby_terminals::family::TerminalFamily::route)
    }

    pub(crate) async fn start(
        bootstrap: &HubDatabaseBootstrap,
        terminals: &RouteLatch,
        home: &Path,
        token: TokenSlot,
    ) -> Result<Self> {
        let mode = terminals.select(crate::front_door::routes::terminal_route(
            &bootstrap.front_door.routes,
        ))?;
        let (retention_stop, retention_shutdown) = watch::channel(false);
        let mut services = Self {
            retention_stop,
            retention: None,
            terminals: None,
        };
        let Some(url) = &bootstrap.database_url else {
            return Ok(services);
        };
        let machine = gobby_core::machine::read_machine_id_from_home(home)?;
        let pool = Pool::build(
            url,
            PoolSettings {
                max_size: 8,
                application_name: "gobby-gdaemon-active".into(),
                acquire_timeout: Duration::from_secs(2),
            },
        )?;
        if mode == SupervisorMode::Owner {
            let options =
                gobby_terminals::host::HostOptions::from_pool(&pool, home, machine.clone()).await?;
            if let Some(options) = options {
                let writes = Pool::build(
                    url,
                    PoolSettings {
                        max_size: 8,
                        application_name: "gobby-gdaemon-terminal-writes".into(),
                        acquire_timeout: Duration::from_secs(2),
                    },
                )?;
                let family = gobby_terminals::family::TerminalFamily::new(
                    terminals,
                    options.clone(),
                    Arc::new(gobby_terminals::host::PostgresEpochAuthority::new(
                        Pool::build(
                            url,
                            PoolSettings {
                                max_size: 8,
                                application_name: "gobby-gdaemon-terminals".into(),
                                acquire_timeout: Duration::from_secs(2),
                            },
                        )?,
                    )),
                )
                .with_write_route(&options, Arc::new(writes), token);
                if let Err(error) = family.start().await {
                    eprintln!("gdaemon terminal host: {error}");
                }
                services.terminals = Some(family);
            }
        }
        services.retention = Some(tokio::spawn(crate::retention::run(
            pool,
            uuid::Uuid::parse_str(&machine)?,
            retention_shutdown,
        )));
        Ok(services)
    }

    pub(crate) async fn stop(self, drain: bool) -> Result<()> {
        self.retention_stop.send_replace(true);
        if let Some(task) = self.retention {
            task.await.context("token-event retention task failed")?;
        }
        if let Some(family) = self.terminals {
            if drain {
                family
                    .drain()
                    .await
                    .context("explicit terminal drain failed")?;
            } else {
                family.stop().await;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_terminals::host::{EpochAuthority, EpochBinding, HostFailure, HostOptions};
    use std::{future::Future, pin::Pin};

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
            std::fs::copy("/bin/sleep", &binary)?;
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
                reporter: Arc::new(|line: &str| eprintln!("{line}")),
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
