//! The process subscriber: the pre-configuration sink until routing activates, then the
//! routes, and the raw stderr writer.
//!
//! A process installs one global subscriber, so the pre-configuration sink and the routes
//! share it: one layer holds the activated routes once, and until then prints each
//! record's message to stderr as bare text, which is the text `eprintln!` wrote before.

use std::sync::OnceLock;

use tracing::subscriber::Interest;
use tracing::{Event, Metadata, Subscriber};
use tracing_subscriber::Registry;
use tracing_subscriber::layer::{Context, Layer, SubscriberExt as _};

use super::Level;
use super::classify::surface_for_target;
use super::layer::{Fields, RouteHandle};

/// Whether this module's subscriber is the process's global one.
static INSTALLED: OnceLock<bool> = OnceLock::new();
/// The routes, once activated.
static ACTIVE: OnceLock<RouteHandle> = OnceLock::new();

/// Installs the sink that prints each record's message to stderr as bare text, until
/// routing activates. Idempotent.
pub fn install_pre_config() {
    installed();
}

fn installed() -> bool {
    *INSTALLED.get_or_init(|| {
        tracing::subscriber::set_global_default(Registry::default().with(ProcessLayer)).is_ok()
    })
}

/// Makes `routes` the process's routes, once per process.
pub(super) fn activate_routes(routes: RouteHandle) -> anyhow::Result<()> {
    anyhow::ensure!(
        installed(),
        "another tracing subscriber is already installed for this process"
    );
    ACTIVE
        .set(routes)
        .map_err(|_| anyhow::anyhow!("routed logging is already active in this process"))
}

/// The process's routes, when routing is active.
pub(super) fn active_routes() -> Option<RouteHandle> {
    ACTIVE.get().cloned()
}

/// Writes `line` to stderr as it is, for output whose bytes other programs read.
#[allow(clippy::print_stderr)]
pub fn raw_stderr(line: &str) {
    eprintln!("{line}");
}

/// The process's one layer.
struct ProcessLayer;

impl<S: Subscriber> Layer<S> for ProcessLayer {
    /// A target outside the `gobby` namespace is never recorded, so its callsites are
    /// disabled once. Levels change with each epoch, so they are checked per record.
    fn register_callsite(&self, metadata: &'static Metadata<'static>) -> Interest {
        if surface_for_target(metadata.target()).is_some() {
            Interest::always()
        } else {
            Interest::never()
        }
    }

    fn enabled(&self, metadata: &Metadata<'_>, _context: Context<'_, S>) -> bool {
        surface_for_target(metadata.target()).is_some()
    }

    fn on_event(&self, event: &Event<'_>, _context: Context<'_, S>) {
        match ACTIVE.get() {
            Some(routes) => routes.route(event),
            None => pre_config(event),
        }
    }
}

/// Before routing activates: the message of each record at INFO and above, verbatim.
fn pre_config(event: &Event<'_>) {
    let printed =
        Level::from_tracing(*event.metadata().level()).is_some_and(|level| level >= Level::Info);
    if printed {
        let mut fields = Fields::default();
        event.record(&mut fields);
        raw_stderr(&fields.message);
    }
}
