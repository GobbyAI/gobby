use crate::config::Context;
use gobby_core::degradation::ServiceState;
use gobby_core::falkor::GraphClient;

use super::GraphReadError;

pub fn require_graph_reads(ctx: &Context) -> anyhow::Result<()> {
    if ctx.falkordb.is_none() {
        return Err(GraphReadError::NotConfigured.into());
    }
    Ok(())
}

pub(super) fn with_required_core_graph<T>(
    ctx: &Context,
    f: impl FnOnce(&mut GraphClient) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    let config = ctx.falkordb.as_ref().ok_or(GraphReadError::NotConfigured)?;
    let connection_config = config.connection_config();
    match gobby_core::falkor::with_graph(
        Some(&connection_config),
        &config.graph_name,
        None,
        |client| f(client).map(Some),
    ) {
        Ok((Some(value), ServiceState::Available)) => Ok(value),
        Ok((_, ServiceState::NotConfigured)) => Err(GraphReadError::NotConfigured.into()),
        Ok((_, ServiceState::Unreachable { message })) => {
            Err(GraphReadError::Unreachable { message }.into())
        }
        Ok((None, ServiceState::Available)) => Err(GraphReadError::QueryFailed {
            message: "graph read returned no value".to_string(),
        }
        .into()),
        Err(error) => Err(GraphReadError::QueryFailed {
            message: error.to_string(),
        }
        .into()),
    }
}
