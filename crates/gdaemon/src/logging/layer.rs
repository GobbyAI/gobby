//! The routed `tracing` layer and the handle that changes its settings.

use std::sync::Arc;

use tracing::Subscriber;
use tracing_subscriber::Layer;

use super::LogSettings;

/// A handle on a routed layer's settings, shared with the epoch applier.
#[derive(Clone, Debug)]
pub(crate) struct RouteHandle {
    _shared: Arc<()>,
}

/// The layer that writes each `gobby.*` record to its surface's files.
#[derive(Debug)]
pub(crate) struct RoutedLayer {
    handle: RouteHandle,
}

impl RoutedLayer {
    /// The handle that applies new settings to this layer.
    pub(crate) fn handle(&self) -> RouteHandle {
        self.handle.clone()
    }
}

impl<S: Subscriber> Layer<S> for RoutedLayer {}

/// A routed layer writing under `settings`.
pub(crate) fn routed_layer(_settings: LogSettings) -> RoutedLayer {
    RoutedLayer {
        handle: RouteHandle {
            _shared: Arc::new(()),
        },
    }
}
