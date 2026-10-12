//! Hub-owned node channels and the reconnecting node client.
pub mod channel;
pub mod registry;

use channel::ChannelTiming;
use gobby_core::postgres_pool::Pool;
use registry::Registry;
use std::sync::Arc;

pub struct HubNodes {
    pub db: Pool,
    pub registry: Registry,
    pub timing: ChannelTiming,
}

#[derive(Clone, Default)]
pub enum NativeRoutes {
    #[default]
    Standalone,
    Hub(Arc<HubNodes>),
}
