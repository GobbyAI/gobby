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
use gobby_core::family::lifecycle::{ActivationEntry, StartFailure, start_families};
use gobby_core::family::{
    BackendStatus, ConfigEpoch, ConfigEpochPublisher, ContextParts, DegradedServices, Extensions,
    FamilyContext, FramePublisher, LeaseState, StartupRecorder, config_channel,
};
use gobby_core::postgres_pool::Pool;
use gobby_core::route_family::RouteFamily;
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tokio::time::MissedTickBehavior;

use crate::front_door::FrontDoorState;
use crate::front_door::registry::activation_plan;
use crate::native_pools::{PoolOutcome, resolve_outcome};

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

/// The whole budget of a family stop phase, and of the unwind after a fatal start or
/// activation. The phase shares it across every deactivate it calls.
pub const FAMILY_STOP_BUDGET: Duration = Duration::from_secs(5);

/// The family name of a start failure that the pool budget caused.
const DATABASE_POOL: &str = "database_pool";

/// The routed target of the start's and the poller's records.
const TARGET: &str = "gobby.daemon";

/// Folds the start's pool outcome into start state. No outcome (no database) degrades
/// nothing; a refusal of the stored values is a start failure no relaunch clears.
pub fn fold_outcome(outcome: Option<PoolOutcome>) -> Result<PoolState, StartFailure> {
    match outcome {
        None => Ok(PoolState {
            budget: None,
            degraded: None,
        }),
        Some(PoolOutcome::Ready(budget)) => Ok(PoolState {
            degraded: budget.degraded,
            budget: Some(budget),
        }),
        Some(PoolOutcome::Degraded { reason }) => Ok(PoolState {
            budget: None,
            degraded: Some(reason),
        }),
        Some(PoolOutcome::Refused { message }) => Err(StartFailure {
            family: DATABASE_POOL,
            message,
            relaunch_could_clear: false,
        }),
    }
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
///
/// The reads run one after another, each on its own checkout: the budget keys, the
/// capacity, then the epoch. A failed epoch read is not fatal: the epoch stays unpublished
/// until the poller reads it. A fatal `start` unwinds the started families within
/// [`FAMILY_STOP_BUDGET`], counted from the moment the unwind begins.
pub async fn prepare(inputs: PrepareInputs) -> Result<ProcessFamilies, StartFailure> {
    let outcome = match &inputs.serve_pool {
        Some(pool) => Some(resolve_outcome(pool, inputs.cpu_count).await),
        None => None,
    };
    let pool = fold_outcome(outcome)?;
    let (config_publisher, config) = config_channel(ConfigEpoch::unpublished());
    let source = Arc::clone(&inputs.epoch_source);
    if let Err(error) = config_publisher.reload(|| source.read()).await {
        tracing::warn!(
            target: TARGET,
            "the active configuration is unreadable at start; it stays unpublished: {error:#}"
        );
    }
    let degraded = DegradedServices::default();
    if let Some(name) = pool.degraded {
        degraded.mark(name);
    }
    let ctx = FamilyContext::new(ContextParts {
        publisher: inputs.publisher,
        backend: inputs.backend,
        lease: inputs.lease,
        budget: pool.budget,
        serve_pool: inputs.serve_pool,
        config,
        home: inputs.home,
        degraded,
        config_publisher,
        startup: inputs.startup,
        extensions: inputs.extensions,
    });
    let activation = activation_plan(&inputs.families, &inputs.routes);
    let report = start_families(&activation, &ctx, FAMILY_STOP_BUDGET).await?;
    for failure in report.ordinary {
        tracing::warn!(
            target: TARGET,
            "family {} started degraded: {}",
            failure.family,
            failure.message
        );
    }
    Ok(ProcessFamilies { ctx, activation })
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
        Box::pin(async move {
            let state = gobby_runtime::config::active_state(&self.pool, &self.token)
                .await
                .map_err(|error| anyhow::anyhow!("reading the active configuration: {error}"))?;
            Ok(match state {
                None => ConfigEpoch::unpublished(),
                Some(state) => ConfigEpoch {
                    revision: state.revision,
                    active_values: Arc::new(state.document.active_values),
                },
            })
        })
    }
}

/// Polls `source` every `every`, each read taken through the publisher's reload, so it
/// starts only once the publisher's lock is held and is published when the epoch changed.
///
/// A failed poll keeps the current epoch. It logs one warning on the transition into
/// failure, one notice on recovery, and the repeated failures in between at debug.
pub fn spawn_config_poller(
    source: Arc<dyn ConfigEpochSource>,
    publisher: Arc<ConfigEpochPublisher>,
    every: Duration,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticks = tokio::time::interval(every);
        ticks.set_missed_tick_behavior(MissedTickBehavior::Delay);
        let mut failing = false;
        loop {
            ticks.tick().await;
            match publisher.reload(|| source.read()).await {
                Ok(_) if failing => {
                    failing = false;
                    tracing::info!(target: TARGET, "the active configuration is readable again");
                }
                Ok(_) => {}
                Err(error) if failing => {
                    tracing::debug!(
                        target: TARGET,
                        "the active configuration poll failed again: {error:#}"
                    );
                }
                Err(error) => {
                    failing = true;
                    tracing::warn!(
                        target: TARGET,
                        "the active configuration poll failed; the current epoch is kept: {error:#}"
                    );
                }
            }
        }
    })
}

/// What a start failure does to the process.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Disposition {
    /// The process stays up, refused, and starts no services.
    Refuse { reason: String },
    /// The process exits nonzero, so a relaunch can clear the cause.
    Exit,
}

/// The disposition of a start failure: one no relaunch clears refuses, with the failing
/// family's name as the reason, and any other exits.
pub fn disposition(failure: &StartFailure) -> Disposition {
    if failure.relaunch_could_clear {
        Disposition::Exit
    } else {
        Disposition::Refuse {
            reason: failure.family.to_owned(),
        }
    }
}

#[cfg(test)]
#[path = "family_start/tests.rs"]
mod tests;
