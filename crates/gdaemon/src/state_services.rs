//! Jobs and terminal authority exist only while the native runtime is active.

use anyhow::{Context, Result};
use gobby_core::{
    bootstrap::{HubDatabaseBootstrap, RouteBackend},
    postgres_pool::{Pool, PoolSettings},
};
use std::{path::Path, sync::Arc, time::Duration};
use tokio::{sync::watch, task::JoinHandle};

pub(crate) struct ActiveServices {
    retention_stop: watch::Sender<bool>,
    retention: Option<JoinHandle<()>>,
    terminals: Option<gobby_terminals::family::TerminalFamily>,
}

impl ActiveServices {
    pub(crate) async fn start(bootstrap: &HubDatabaseBootstrap, home: &Path) -> Result<Self> {
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
        if bootstrap.front_door.routes.get("terminal_ws") == Some(&RouteBackend::Native) {
            let options =
                gobby_terminals::host::HostOptions::from_pool(&pool, home, machine.clone()).await?;
            if let Some(options) = options {
                let family = gobby_terminals::family::TerminalFamily::new(
                    RouteBackend::Native,
                    options,
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
                );
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

    pub(crate) async fn stop(self) -> Result<()> {
        self.retention_stop.send_replace(true);
        if let Some(task) = self.retention {
            task.await.context("token-event retention task failed")?;
        }
        if let Some(family) = self.terminals {
            family.stop().await;
        }
        Ok(())
    }
}
