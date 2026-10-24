//! Route mode selects one lifecycle owner; observers cannot acquire control ownership.
use std::sync::Arc;

use gobby_core::bootstrap::RouteBackend;
use gobby_core::postgres_pool::Pool;

use crate::host::{EpochAuthority, HostFailure, HostInfo, HostOptions, HostSupervisor};
use crate::leases::Registry;
use crate::native_write::NativeWrite;
use crate::tmux_write::{SystemTmux, TmuxWrite};
use crate::write::{Coordinator, Runtime, Runtimes};
use crate::write_api::{Route, TokenSlot};

/// Lifecycle events buffered before publication faults.
const LIFECYCLE_CAPACITY: usize = 256;

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
    route: Option<Arc<Route>>,
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
            route: None,
        }
    }

    /// Publishes the loopback write route in Owner mode. Its native runtime
    /// dispatches through `options`, and it admits tokens from `token`.
    pub fn with_write_route(
        mut self,
        options: &HostOptions,
        pool: Arc<Pool>,
        token: TokenSlot,
    ) -> Self {
        if self.supervisor.is_none() {
            return self;
        }
        let (leases, mut lifecycle) = Registry::new(LIFECYCLE_CAPACITY);
        // Nothing consumes lifecycle events yet; draining them keeps publication from faulting.
        tokio::spawn(async move { while lifecycle.recv().await.is_some() {} });
        let native: Arc<dyn Runtime> = Arc::new(NativeWrite::new(options.clone()));
        let tmux: Arc<dyn Runtime> = Arc::new(TmuxWrite::new(Arc::new(SystemTmux)));
        let runtimes = Runtimes {
            native: Some(native),
            tmux: Some(tmux),
        };
        let coordinator = Coordinator::new(pool, Arc::new(leases), runtimes);
        self.route = Some(Arc::new(Route::new(Arc::new(coordinator), token)));
        self
    }

    /// The write route, present while this family owns the terminals.
    pub fn route(&self) -> Option<Arc<Route>> {
        self.route.clone()
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
