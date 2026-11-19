//! The family context, lifecycle hooks and job shape that route families share.
//!
//! The daemon builds one [`FamilyContext`] per process and hands it to every family hook
//! and claim handler. A family reads its handles through the context and never through
//! daemon types.

pub mod config_epoch;
pub mod degraded;
pub mod identity;
pub mod lifecycle;
pub mod logger;
pub mod publisher;
pub mod status;

#[cfg(test)]
mod tests;

use std::any::{Any, TypeId};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::http::request::Parts;
use tokio::sync::watch;

use crate::database_concurrency::pool_budget::PoolBudget;
use crate::postgres_pool::Pool;

pub use config_epoch::{ConfigEpoch, ConfigEpochPublisher, config_channel};
pub use degraded::DegradedServices;
pub use identity::{CallerIdentity, IDENTITY_ENTRY_HEADERS, strip_identity_entry_headers};
pub use logger::FamilyLogger;
pub use publisher::{FramePublisher, PublishRefused};
pub use status::{BackendStatus, LeaseState};

/// Records the steps of the daemon's startup sequence.
pub trait StartupRecorder: Send + Sync {
    /// Records that `family` reached `step` with `outcome`.
    fn record_step(&self, family: &str, step: &str, outcome: &str);
}

/// Typed handles that later chunks add to the context without gcore naming their types.
#[derive(Clone, Default)]
pub struct Extensions {
    values: Arc<HashMap<TypeId, Arc<dyn Any + Send + Sync>>>,
}

impl Extensions {
    /// An empty bag.
    pub fn new() -> Self {
        Self::default()
    }

    /// Stores `value`, replacing any earlier value of the same type.
    pub fn insert<T: Send + Sync + 'static>(&mut self, value: T) {
        Arc::make_mut(&mut self.values).insert(TypeId::of::<T>(), Arc::new(value));
    }
}

/// Everything a [`FamilyContext`] is built from.
pub struct ContextParts {
    /// Broadcasts frames to WebSocket subscribers.
    pub publisher: Arc<dyn FramePublisher>,
    /// The backend supervisor's status; `None` on a door with no runtime.
    pub backend: Option<watch::Receiver<BackendStatus>>,
    /// The hub lease state; `None` when the daemon holds no lease.
    pub lease: Option<watch::Receiver<LeaseState>>,
    /// The resolved pool budget; `None` without a database.
    pub budget: Option<PoolBudget>,
    /// The serve pool; `None` without a database.
    pub serve_pool: Option<Arc<Pool>>,
    /// The configuration epoch, as the config publisher moves it.
    pub config: watch::Receiver<ConfigEpoch>,
    /// The daemon's home directory.
    pub home: PathBuf,
    /// The degraded set the context starts with.
    pub degraded: DegradedServices,
    /// The one publisher of the configuration epoch.
    pub config_publisher: Arc<ConfigEpochPublisher>,
    /// The startup recorder, when one is attached.
    pub startup: Option<Arc<dyn StartupRecorder>>,
    /// Typed handles for later chunks.
    pub extensions: Extensions,
}

struct Inner {
    publisher: Arc<dyn FramePublisher>,
    home: PathBuf,
    degraded: DegradedServices,
    config_publisher: Arc<ConfigEpochPublisher>,
}

/// The process's handles, shared by every family. Cloning shares the same handles.
#[derive(Clone)]
pub struct FamilyContext {
    inner: Arc<Inner>,
}

/// The publisher of a context with no broker: it refuses every frame.
struct NoBroker;

impl FramePublisher for NoBroker {
    fn publish_frames(
        &self,
        _filter: &serde_json::Value,
        _frames: Vec<String>,
    ) -> Result<(), PublishRefused> {
        Err(PublishRefused)
    }
}

impl FamilyContext {
    /// Builds the context from its parts.
    pub fn new(parts: ContextParts) -> Self {
        Self {
            inner: Arc::new(Inner {
                publisher: parts.publisher,
                home: parts.home,
                degraded: parts.degraded,
                config_publisher: parts.config_publisher,
            }),
        }
    }

    /// A context with no handles: no broker, backend, lease, budget or pool, the
    /// unpublished epoch and an empty degraded set.
    pub fn unconfigured() -> Self {
        let (config_publisher, config) = config_channel(ConfigEpoch::unpublished());
        Self::new(ContextParts {
            publisher: Arc::new(NoBroker),
            backend: None,
            lease: None,
            budget: None,
            serve_pool: None,
            config,
            home: PathBuf::new(),
            degraded: DegradedServices::default(),
            config_publisher,
            startup: None,
            extensions: Extensions::new(),
        })
    }

    /// The frame publisher.
    pub fn publisher(&self) -> Arc<dyn FramePublisher> {
        Arc::clone(&self.inner.publisher)
    }

    /// The backend status now, when the door has a runtime.
    pub fn backend_status(&self) -> Option<BackendStatus> {
        None
    }

    /// A receiver of the backend status, when the door has a runtime.
    pub fn subscribe_backend(&self) -> Option<watch::Receiver<BackendStatus>> {
        None
    }

    /// The lease state now, when the daemon holds a lease.
    pub fn lease_state(&self) -> Option<LeaseState> {
        None
    }

    /// The resolved pool budget, when there is a database.
    pub fn budget(&self) -> Option<&PoolBudget> {
        None
    }

    /// The serve pool, when there is a database.
    pub fn serve_pool(&self) -> Option<Arc<Pool>> {
        None
    }

    /// The daemon's home directory.
    pub fn home(&self) -> &Path {
        &self.inner.home
    }

    /// The configuration epoch now.
    pub fn config_epoch(&self) -> ConfigEpoch {
        ConfigEpoch::unpublished()
    }

    /// A receiver that sees each later configuration epoch.
    pub fn subscribe_config(&self) -> watch::Receiver<ConfigEpoch> {
        config_channel(ConfigEpoch::unpublished()).1
    }

    /// The logging namespace and metric label of `family`.
    pub fn logger(&self, family: &'static str) -> FamilyLogger {
        FamilyLogger::new(family)
    }

    /// The degraded set.
    pub fn degraded(&self) -> &DegradedServices {
        &self.inner.degraded
    }

    /// The identity that authentication wrote into the request. A request that did not
    /// pass authentication with an API key has none.
    pub fn caller(&self, _parts: &Parts) -> Option<CallerIdentity> {
        None
    }

    /// The one configuration publisher.
    pub fn config_publisher(&self) -> Arc<ConfigEpochPublisher> {
        Arc::clone(&self.inner.config_publisher)
    }

    /// The startup recorder, when one is attached.
    pub fn startup(&self) -> Option<Arc<dyn StartupRecorder>> {
        None
    }

    /// The extension of type `T`, when one was inserted.
    pub fn extension<T: Send + Sync + 'static>(&self) -> Option<Arc<T>> {
        None
    }
}
