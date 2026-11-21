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
use tokio::sync::{oneshot, watch};
use tokio::task::JoinHandle;
use tokio::time::MissedTickBehavior;

use crate::front_door::FrontDoorState;
use crate::front_door::registry::activation_plan;
use crate::native_pools::{PoolOutcome, resolve_outcome};

/// The process's family context and the entries its lease activates.
pub struct ProcessFamilies {
    pub ctx: FamilyContext,
    pub activation: Vec<ActivationEntry<FrontDoorState>>,
    /// Whether the start's configuration read succeeded. A read that finds no stored row
    /// succeeded: it is the unpublished epoch.
    pub config_read: bool,
    /// The start's warnings, which routed logging replays once it activates.
    pub start_warnings: Vec<String>,
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
/// until the poller reads it, and [`ProcessFamilies::config_read`] says so. The start's
/// warnings are emitted as they happen and kept in [`ProcessFamilies::start_warnings`].
/// A fatal `start` unwinds the started families within
/// [`FAMILY_STOP_BUDGET`], counted from the moment the unwind begins.
pub async fn prepare(inputs: PrepareInputs) -> Result<ProcessFamilies, StartFailure> {
    let outcome = match &inputs.serve_pool {
        Some(pool) => Some(resolve_outcome(pool, inputs.cpu_count).await),
        None => None,
    };
    prepare_from(inputs, outcome).await
}

/// [`prepare`] after the pool budget read, with its `outcome`.
async fn prepare_from(
    inputs: PrepareInputs,
    outcome: Option<PoolOutcome>,
) -> Result<ProcessFamilies, StartFailure> {
    let pool = fold_outcome(outcome)?;
    let mut start_warnings = Vec::new();
    let (config_publisher, config) = config_channel(ConfigEpoch::unpublished());
    let source = Arc::clone(&inputs.epoch_source);
    let config_read = match config_publisher.reload(|| source.read()).await {
        Ok(_) => true,
        Err(error) => {
            start_warning(
                &mut start_warnings,
                format!(
                    "the active configuration is unreadable at start; it stays unpublished: \
                     {error:#}"
                ),
            );
            false
        }
    };
    let degraded = DegradedServices::default();
    if let Some(name) = pool.degraded {
        degraded.mark(name);
    }
    if let Some(budget) = &pool.budget
        && let Some(name) = budget.degraded
    {
        start_warning(
            &mut start_warnings,
            format!(
                "the native pool budget is {} connections, below 8; {name} is degraded and \
                 each native pool keeps its share of {}",
                budget.native_budget, budget.share
            ),
        );
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
        start_warning(
            &mut start_warnings,
            format!(
                "family {} started degraded: {}",
                failure.family, failure.message
            ),
        );
    }
    Ok(ProcessFamilies {
        ctx,
        activation,
        config_read,
        start_warnings,
    })
}

/// Waits for the first successful configuration read, then runs `activate` with the epoch
/// current at that moment. `first_read` is None when the start's own read succeeded, and
/// otherwise fires on the poller's first successful read. A poller that stops before
/// reading successfully is an error, and `activate` does not run.
pub async fn activate_after_first_read<T>(
    first_read: Option<oneshot::Receiver<()>>,
    config: watch::Receiver<ConfigEpoch>,
    activate: impl FnOnce(ConfigEpoch) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    if let Some(first_read) = first_read {
        first_read.await.map_err(|_| {
            anyhow::anyhow!("the configuration poller stopped before its first successful read")
        })?;
    }
    let current = config.borrow().clone();
    activate(current)
}

/// Emits `warning` now, which before routed logging activates reaches stderr, and keeps
/// it for [`replay_start_warnings`].
fn start_warning(warnings: &mut Vec<String>, warning: String) {
    tracing::warn!(target: TARGET, "{warning}");
    warnings.push(warning);
}

/// Writes the start's warnings again, for routed logging that activated after them. Each
/// reaches `errors.log` whatever the stored level, which may be above WARNING.
pub fn replay_start_warnings(warnings: &[String]) {
    for warning in warnings {
        crate::logging::replay_warning(TARGET, module_path!(), warning);
    }
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
///
/// `first_read`, when given, fires once, after the poller's first successful read, even
/// when that read published nothing (a read of no stored row is the unpublished epoch).
pub fn spawn_config_poller(
    source: Arc<dyn ConfigEpochSource>,
    publisher: Arc<ConfigEpochPublisher>,
    every: Duration,
    mut first_read: Option<oneshot::Sender<()>>,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticks = tokio::time::interval(every);
        ticks.set_missed_tick_behavior(MissedTickBehavior::Delay);
        let mut failing = false;
        loop {
            ticks.tick().await;
            let read = publisher.reload(|| source.read()).await;
            if read.is_ok()
                && let Some(first_read) = first_read.take()
            {
                // The receiver may be gone; nothing then waits on the signal.
                let _ = first_read.send(());
            }
            match read {
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
