//! Route mode selects one lifecycle owner; observers cannot acquire control ownership.
use std::sync::Arc;

use gobby_core::bootstrap::RouteBackend;

use crate::host::{EpochAuthority, HostFailure, HostInfo, HostOptions, HostSupervisor};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SupervisorMode {
    Observer,
    Owner,
}

/// Whether gdaemon serves the terminal WS routes natively.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WsRoutes {
    Registered,
    Unregistered,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum RouteModeError {
    #[error(
        "front_door.routes.terminal_ws: native needs terminal WS routes this gdaemon does not register; use proxy or compare"
    )]
    FamilyUnregistered,
    #[error(
        "front_door.routes.terminal_ws changed from {started:?} to {requested:?}; restart gdaemon to apply it"
    )]
    RestartRequired {
        started: RouteBackend,
        requested: RouteBackend,
    },
}

/// The terminal route mode, read once per process. Ownership follows it, and
/// a later selection of another mode is refused until restart.
pub struct RouteLatch {
    route: RouteBackend,
}

impl RouteLatch {
    pub fn new(route: RouteBackend, ws: WsRoutes) -> Result<Self, RouteModeError> {
        if route == RouteBackend::Native && ws == WsRoutes::Unregistered {
            return Err(RouteModeError::FamilyUnregistered);
        }
        Ok(Self { route })
    }

    pub fn route(&self) -> RouteBackend {
        self.route
    }

    pub fn mode(&self) -> SupervisorMode {
        match self.route {
            RouteBackend::Native => SupervisorMode::Owner,
            RouteBackend::Proxy | RouteBackend::Compare => SupervisorMode::Observer,
        }
    }

    pub fn select(&self, requested: RouteBackend) -> Result<SupervisorMode, RouteModeError> {
        if requested != self.route {
            return Err(RouteModeError::RestartRequired {
                started: self.route,
                requested,
            });
        }
        Ok(self.mode())
    }
}

pub struct TerminalFamily {
    supervisor: Option<HostSupervisor>,
}

impl TerminalFamily {
    pub fn new(
        latch: &RouteLatch,
        options: HostOptions,
        authority: Arc<dyn EpochAuthority>,
    ) -> Self {
        Self {
            supervisor: match latch.mode() {
                SupervisorMode::Owner => Some(HostSupervisor::new(options, authority)),
                SupervisorMode::Observer => None,
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
