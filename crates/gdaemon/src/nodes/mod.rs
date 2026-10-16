//! Hub-owned node channels and the reconnecting node client.
pub mod channel;
pub mod hooks;
pub mod machines_api;
pub mod registry;

use channel::ChannelTiming;
use gobby_core::postgres_pool::Pool;
use registry::Registry;
use std::sync::Arc;

pub struct HubNodes {
    pub db: Arc<Pool>,
    pub registry: Registry,
    pub timing: ChannelTiming,
}

#[derive(Clone)]
pub enum NativeRoutes {
    Standalone(Option<Arc<Pool>>),
    Hub(Arc<HubNodes>),
}

impl Default for NativeRoutes {
    fn default() -> Self {
        Self::Standalone(None)
    }
}
