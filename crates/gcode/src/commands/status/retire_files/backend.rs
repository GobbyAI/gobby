//! Credential-free binding to the endpoints selected by the canonical grant.

use anyhow::{Context as _, ensure};
use serde::{Deserialize, Serialize};

use crate::config::Context;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct BackendIdentity {
    pub postgres_host: String,
    pub postgres_port: u16,
    pub postgres_database: String,
    pub qdrant_url: String,
    pub falkor_host: String,
    pub falkor_port: u16,
    pub falkor_graph: String,
}

impl BackendIdentity {
    pub(super) fn from_context(ctx: &Context) -> anyhow::Result<Self> {
        let postgres: postgres::Config = ctx.database_url.parse()?;
        let [postgres::config::Host::Tcp(host)] = postgres.get_hosts() else {
            anyhow::bail!("retirement requires one explicit PostgreSQL TCP host");
        };
        ensure!(
            postgres.get_ports().len() <= 1,
            "multiple PostgreSQL ports refused"
        );
        let graph = ctx
            .falkordb
            .as_ref()
            .context("graph backend required for retirement")?;
        let vector = ctx
            .qdrant
            .as_ref()
            .and_then(|value| value.url.as_ref())
            .context("vector backend required for retirement")?;
        let identity = Self {
            postgres_host: host.clone(),
            postgres_port: postgres.get_ports().first().copied().unwrap_or(5432),
            postgres_database: postgres
                .get_dbname()
                .context("explicit PostgreSQL database required")?
                .to_string(),
            qdrant_url: vector.trim_end_matches('/').to_string(),
            falkor_host: graph.host.clone(),
            falkor_port: graph.port,
            falkor_graph: graph.graph_name.clone(),
        };
        identity.validate()?;
        Ok(identity)
    }

    pub(super) fn validate(&self) -> anyhow::Result<()> {
        ensure!(
            !self.postgres_host.is_empty()
                && !self.postgres_database.is_empty()
                && !self.falkor_host.is_empty()
                && !self.falkor_graph.is_empty()
                && self.postgres_port > 0
                && self.falkor_port > 0,
            "incomplete backend identity"
        );
        let url = reqwest::Url::parse(&self.qdrant_url)?;
        ensure!(
            matches!(url.scheme(), "http" | "https")
                && url.host_str().is_some()
                && url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.fragment().is_none()
                && !self.qdrant_url.ends_with('/'),
            "Qdrant identity must be a credential-free endpoint without trailing slash"
        );
        Ok(())
    }
}
