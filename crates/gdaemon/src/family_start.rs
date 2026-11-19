//! The process's family start: the pool outcome folded into start state, the family
//! context and activation plan prepared before the doors bind, the configuration poller,
//! and what a start failure does to the process.

use std::collections::BTreeMap;
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use gobby_core::bootstrap::RouteBackend;
use gobby_core::database_concurrency::pool_budget::PoolBudget;
use gobby_core::family::lifecycle::{ActivationEntry, StartFailure};
use gobby_core::family::{
    BackendStatus, ConfigEpoch, ConfigEpochPublisher, Extensions, FamilyContext, FramePublisher,
    LeaseState, StartupRecorder,
};
use gobby_core::postgres_pool::Pool;
use gobby_core::route_family::RouteFamily;
use tokio::sync::watch;
use tokio::task::JoinHandle;

use crate::front_door::FrontDoorState;
use crate::native_pools::PoolOutcome;

/// The process's family context and the entries its lease activates.
pub struct ProcessFamilies {
    pub ctx: FamilyContext,
    pub activation: Vec<ActivationEntry<FrontDoorState>>,
}

/// The pool budget the start resolved, and the degraded mark it leaves.
#[derive(Debug, Clone, PartialEq)]
pub struct PoolState {
    pub budget: Option<PoolBudget>,
    pub degraded: Option<&'static str>,
}

/// Folds the start's pool outcome into start state. No outcome (no database) degrades
/// nothing; a refusal of the stored values is a start failure.
pub fn fold_outcome(_outcome: Option<PoolOutcome>) -> Result<PoolState, StartFailure> {
    Ok(PoolState {
        budget: None,
        degraded: None,
    })
}

/// Everything [`prepare`] builds the process's families from.
pub struct PrepareInputs {
    pub serve_pool: Option<Arc<Pool>>,
    pub home: PathBuf,
    pub routes: BTreeMap<String, RouteBackend>,
    pub publisher: Arc<dyn FramePublisher>,
    pub backend: Option<watch::Receiver<BackendStatus>>,
    pub lease: Option<watch::Receiver<LeaseState>>,
    pub epoch_source: Arc<dyn ConfigEpochSource>,
    pub cpu_count: u32,
    /// The one family list the serve path built.
    pub families: Vec<&'static dyn RouteFamily<FrontDoorState>>,
    pub extensions: Extensions,
    pub startup: Option<Arc<dyn StartupRecorder>>,
}

/// Reads the pool budget and the configuration epoch, builds the family context and the
/// activation plan, and runs every family's `start`.
pub async fn prepare(_inputs: PrepareInputs) -> Result<ProcessFamilies, StartFailure> {
    Err(StartFailure {
        family: "unimplemented",
        message: String::new(),
        relaunch_could_clear: false,
    })
}

/// Reads the stored configuration epoch.
pub trait ConfigEpochSource: Send + Sync {
    fn read(&self) -> Pin<Box<dyn Future<Output = anyhow::Result<ConfigEpoch>> + Send + '_>>;
}

/// The production epoch source: the active configuration state on the hub.
pub struct ActiveConfigSource {
    pub pool: Arc<Pool>,
    pub token: String,
}

impl ConfigEpochSource for ActiveConfigSource {
    fn read(&self) -> Pin<Box<dyn Future<Output = anyhow::Result<ConfigEpoch>> + Send + '_>> {
        Box::pin(async {
            Err(anyhow::anyhow!(
                "the active configuration read is not wired"
            ))
        })
    }
}

/// Polls `source` every `every` and reloads the publisher with each read.
pub fn spawn_config_poller(
    _source: Arc<dyn ConfigEpochSource>,
    _publisher: Arc<ConfigEpochPublisher>,
    _every: Duration,
) -> JoinHandle<()> {
    tokio::spawn(async {})
}

/// What a start failure does to the process.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Disposition {
    /// The process stays up, refused, and starts no services.
    Refuse { reason: String },
    /// The process exits nonzero, so a relaunch can clear the cause.
    Exit,
}

/// The disposition of a start failure.
pub fn disposition(_failure: &StartFailure) -> Disposition {
    Disposition::Exit
}

#[cfg(test)]
#[path = "family_start/tests.rs"]
mod tests;
