//! Route mode selects one lifecycle owner; observers cannot acquire control ownership.
use std::sync::Arc;

use gobby_core::bootstrap::RouteBackend;

use crate::host::{EpochAuthority, HostFailure, HostInfo, HostOptions, HostSupervisor};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SupervisorMode {
    Observer,
    Owner,
}

pub struct TerminalFamily {
    supervisor: Option<HostSupervisor>,
}

impl TerminalFamily {
    pub fn new(
        route: RouteBackend,
        options: HostOptions,
        authority: Arc<dyn EpochAuthority>,
    ) -> Self {
        Self {
            supervisor: match route {
                RouteBackend::Native => Some(HostSupervisor::new(options, authority)),
                RouteBackend::Proxy | RouteBackend::Compare => None,
            },
        }
    }

    pub fn mode(&self) -> SupervisorMode {
        if self.supervisor.is_some() {
            SupervisorMode::Owner
        } else {
            SupervisorMode::Observer
        }
    }

    pub async fn start(&self) -> Result<Option<HostInfo>, HostFailure> {
        match &self.supervisor {
            Some(supervisor) => supervisor.start().await.map(Some),
            None => Ok(None),
        }
    }

    pub async fn stop(&self) {
        if let Some(supervisor) = &self.supervisor {
            supervisor.stop().await;
        }
    }

    pub async fn drain(&self) -> Result<(), HostFailure> {
        if let Some(supervisor) = &self.supervisor {
            supervisor.drain().await?;
        }
        Ok(())
    }
}
