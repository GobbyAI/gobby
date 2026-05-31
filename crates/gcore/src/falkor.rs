//! FalkorDB foundation adapter boundary.
//!
//! This module is available with the `falkor` feature. The feature also enables
//! `urlencoding` so FalkorDB connection URLs can encode passwords safely when
//! graph client construction is added.

use std::collections::HashMap;

use falkordb::{
    FalkorClientBuilder, FalkorConnectionInfo, FalkorValue, LazyResultSet, QueryBuilder,
    QueryResult, SyncGraph,
};
use serde_json::{Map, Number, Value};

use crate::config::FalkorConfig;
use crate::degradation::ServiceState;

/// Row from a FalkorDB query response.
pub type Row = HashMap<String, Value>;

/// Blocking FalkorDB graph client.
///
/// Owns a connection to a named graph. Domain crates supply Cypher queries;
/// this adapter handles connection lifecycle and result parsing.
pub struct GraphClient {
    graph: SyncGraph,
}

/// Read-only view of a synchronous FalkorDB graph.
///
/// `falkordb` requires mutable access even for `GRAPH.RO_QUERY`; this wrapper
/// exposes only the read-only query surface instead of the raw mutable graph.
pub struct ReadOnlySyncGraph<'a> {
    graph: &'a mut SyncGraph,
}

impl<'a> ReadOnlySyncGraph<'a> {
    /// Return the selected graph name.
    pub fn graph_name(&self) -> &str {
        self.graph.graph_name()
    }

    /// Create a read-only FalkorDB query builder.
    pub fn ro_query<'b>(
        &'b mut self,
        query_string: &'b str,
    ) -> QueryBuilder<'b, QueryResult<LazyResultSet<'b>>, &'b str, SyncGraph> {
        self.graph.ro_query(query_string)
    }
}

impl GraphClient {
    /// Build a client for a consumer-selected graph.
    pub fn from_config(config: &FalkorConfig, graph_name: &str) -> anyhow::Result<Self> {
        let password = config.password.as_deref().unwrap_or_default();
        let url = format!(
            "falkor://:{}@{}:{}",
            urlencoding::encode(password),
            config.host,
            config.port,
        );
        let conn_info: FalkorConnectionInfo = url.as_str().try_into()?;
        let client = FalkorClientBuilder::new()
            .with_connection_info(conn_info)
            .build()?;
        Ok(Self {
            graph: client.select_graph(graph_name),
        })
    }

    /// Run a read-only closure with the underlying synchronous FalkorDB graph.
    ///
    /// This is an escape hatch for consumers that need a FalkorDB operation the
    /// shared `GraphClient` API does not expose yet. It intentionally exposes a
    /// wrapper limited to read-only queries.
    pub fn with_sync_graph<T>(
        &mut self,
        f: impl FnOnce(&mut ReadOnlySyncGraph<'_>) -> anyhow::Result<T>,
    ) -> anyhow::Result<T> {
        let mut graph = ReadOnlySyncGraph {
            graph: &mut self.graph,
        };
        f(&mut graph)
    }

    /// Execute a Cypher query and return parsed rows.
    pub fn query(
        &mut self,
        cypher: &str,
        params: Option<HashMap<String, String>>,
    ) -> anyhow::Result<Vec<Row>> {
        match params {
            Some(params) => {
                let result = self.graph.query(cypher).with_params(&params).execute()?;
                Ok(parse_falkor_result(result))
            }
            None => {
                let result = self.graph.query(cypher).execute()?;
                Ok(parse_falkor_result(result))
            }
        }
    }

    /// Ensure an exact node index exists for a label/property pair.
    pub fn ensure_exact_node_index(&mut self, label: &str, property: &str) -> anyhow::Result<()> {
        let cypher = format!(
            "CREATE INDEX ON :{}({})",
            escape_label(label),
            escape_property(property)
        );
        match self.query(&cypher, None) {
            Ok(_) => Ok(()),
            Err(error) if is_existing_index_error(&error) => Ok(()),
            Err(error) => Err(error),
        }
    }
}

/// Run a closure with a FalkorDB client, with typed degradation.
///
/// Degradation contract:
/// - missing config returns the caller default with `ServiceState::NotConfigured`
/// - connection failure returns the caller default with `ServiceState::Unreachable`
/// - a successful closure returns its value with `ServiceState::Available`
/// - a closure error is propagated to the caller
pub fn with_graph<T>(
    config: Option<&FalkorConfig>,
    graph_name: &str,
    default: T,
    f: impl FnOnce(&mut GraphClient) -> anyhow::Result<T>,
) -> anyhow::Result<(T, ServiceState)> {
    with_graph_client(config, graph_name, default, GraphClient::from_config, f)
}

fn with_graph_client<T, C>(
    config: Option<&FalkorConfig>,
    graph_name: &str,
    default: T,
    make_client: impl FnOnce(&FalkorConfig, &str) -> anyhow::Result<C>,
    f: impl FnOnce(&mut C) -> anyhow::Result<T>,
) -> anyhow::Result<(T, ServiceState)> {
    let Some(config) = config else {
        return Ok((default, ServiceState::NotConfigured));
    };

    let mut client = match make_client(config, graph_name) {
        Ok(client) => client,
        Err(error) => {
            return Ok((
                default,
                ServiceState::Unreachable {
                    message: error.to_string(),
                },
            ));
        }
    };

    let value = f(&mut client)?;
    Ok((value, ServiceState::Available))
}

/// Escape a graph label for safe Cypher embedding.
pub fn escape_label(label: &str) -> String {
    escape_identifier(label)
}

/// Escape a relationship type for safe Cypher embedding.
pub fn escape_rel_type(rel: &str) -> String {
    escape_identifier(rel)
}

/// Escape a property key for safe Cypher embedding.
pub fn escape_property(key: &str) -> String {
    escape_identifier(key)
}

/// Escape a string parameter value for Cypher.
pub fn escape_string(value: &str) -> String {
    let escaped = value.replace('\\', "\\\\").replace('\'', "\\'");
    format!("'{escaped}'")
}

fn escape_identifier(value: &str) -> String {
    format!("`{}`", value.replace('`', "``"))
}

fn is_existing_index_error(error: &anyhow::Error) -> bool {
    let message = error.to_string().to_ascii_lowercase();
    message.contains("already indexed")
        || message.contains("already exists")
        || message.contains("index already exists")
}

fn parse_falkor_result(result: QueryResult<LazyResultSet<'_>>) -> Vec<Row> {
    parse_falkor_records(result.header, result.data)
}

fn parse_falkor_records<I>(headers: Vec<String>, records: I) -> Vec<Row>
where
    I: IntoIterator<Item = Vec<FalkorValue>>,
{
    records
        .into_iter()
        .map(|record| {
            let mut row = HashMap::new();
            for (i, field) in headers.iter().enumerate() {
                let value = record.get(i).cloned().unwrap_or(FalkorValue::None);
                row.insert(field.clone(), falkor_value_to_json(value));
            }
            row
        })
        .collect()
}

fn falkor_value_to_json(value: FalkorValue) -> Value {
    match value {
        FalkorValue::String(value) => Value::String(value),
        FalkorValue::Bool(value) => Value::Bool(value),
        FalkorValue::I64(value) => Value::Number(Number::from(value)),
        FalkorValue::F64(value) => Number::from_f64(value)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        FalkorValue::Array(values) => Value::Array(
            values
                .into_iter()
                .map(falkor_value_to_json)
                .collect::<Vec<_>>(),
        ),
        FalkorValue::Map(values) => Value::Object(
            values
                .into_iter()
                .map(|(key, value)| (key, falkor_value_to_json(value)))
                .collect::<Map<_, _>>(),
        ),
        FalkorValue::None => Value::Null,
        value => Value::String(format!("{value:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::FalkorConfig;
    use crate::degradation::ServiceState;
    use anyhow::anyhow;

    struct FakeGraphClient;

    fn test_config() -> FalkorConfig {
        FalkorConfig {
            host: "127.0.0.1".to_string(),
            port: 1,
            password: None,
        }
    }

    #[test]
    fn with_graph_degradation_contract() {
        let default = vec!["default".to_string()];
        let missing = with_graph::<Vec<String>>(None, "consumer_graph", default.clone(), |_| {
            unreachable!("missing config should not construct a client")
        })
        .expect("missing config should degrade");
        assert_eq!(missing, (default.clone(), ServiceState::NotConfigured));

        let unreachable = with_graph_client(
            Some(&test_config()),
            "consumer_graph",
            default.clone(),
            |_config, _graph_name| Err(anyhow!("connection refused")),
            |_client: &mut FakeGraphClient| Ok(vec!["value".to_string()]),
        )
        .expect("connection failure should degrade");
        assert!(matches!(
            unreachable,
            (value, ServiceState::Unreachable { ref message })
                if value == default && message.contains("connection refused")
        ));

        let available = with_graph_client(
            Some(&test_config()),
            "consumer_graph",
            default.clone(),
            |_config, _graph_name| Ok(FakeGraphClient),
            |_client| Ok(vec!["value".to_string()]),
        )
        .expect("successful closure should return available state");
        assert_eq!(
            available,
            (vec!["value".to_string()], ServiceState::Available)
        );

        let propagated = with_graph_client(
            Some(&test_config()),
            "consumer_graph",
            default,
            |_config, _graph_name| Ok(FakeGraphClient),
            |_client| Err::<Vec<String>, _>(anyhow!("query failed")),
        );
        assert_eq!(
            propagated
                .expect_err("closure error should propagate")
                .to_string(),
            "query failed"
        );
    }

    #[test]
    fn escapes_graph_tokens() {
        assert_eq!(escape_label("Node`Label"), "`Node``Label`");
        assert_eq!(escape_rel_type("REL`OUT"), "`REL``OUT`");
        assert_eq!(escape_property("line`start"), "`line``start`");
        assert_eq!(
            escape_string("module\\path'symbol"),
            "'module\\\\path\\'symbol'"
        );
    }

    #[test]
    fn no_domain_labels_in_adapter() {
        let source = include_str!("falkor.rs");
        let forbidden = [
            ["Code", "Symbol"].concat(),
            ["CA", "LLS"].concat(),
            ["IM", "PORTS"].concat(),
            ["Wiki", "Doc"].concat(),
            ["LINKS", "_TO"].concat(),
        ];

        for token in forbidden {
            assert!(!source.contains(&token), "{token} leaked into adapter");
        }
    }

    #[test]
    fn graph_unavailable_is_not_empty_success() {
        let unavailable = with_graph_client(
            Some(&test_config()),
            "consumer_graph",
            Vec::<Row>::new(),
            |_config, _graph_name| Err(anyhow!("dial tcp failed")),
            |_client: &mut FakeGraphClient| Ok(vec![Row::new()]),
        )
        .expect("connection failure should degrade");

        assert!(matches!(
            unavailable,
            (rows, ServiceState::Unreachable { .. }) if rows.is_empty()
        ));

        let empty_success = with_graph_client(
            Some(&test_config()),
            "consumer_graph",
            vec![Row::new()],
            |_config, _graph_name| Ok(FakeGraphClient),
            |_client| Ok(Vec::<Row>::new()),
        )
        .expect("successful empty query should be available");

        assert_eq!(empty_success, (Vec::<Row>::new(), ServiceState::Available));
    }

    #[test]
    fn graph_name_is_consumer_supplied() {
        let mut selected_graph = None;
        let result = with_graph_client(
            Some(&test_config()),
            "consumer_graph",
            (),
            |_config, graph_name| {
                selected_graph = Some(graph_name.to_string());
                Ok(FakeGraphClient)
            },
            |_client| Ok(()),
        )
        .expect("graph selection should succeed");

        assert_eq!(result, ((), ServiceState::Available));
        assert_eq!(selected_graph.as_deref(), Some("consumer_graph"));

        let source = include_str!("falkor.rs");
        let code_graph_name = ["gobby", "_code"].concat();
        assert!(
            !source.contains(&code_graph_name),
            "adapter must not hardcode a consumer graph name"
        );
    }

    #[test]
    fn live_sync_graph_read_is_env_gated() {
        let Some((config, graph_name)) = live_falkor_fixture() else {
            eprintln!("skipping live FalkorDB read test: GOBBY_FALKORDB_HOST is not set");
            return;
        };

        let Ok(mut client) = GraphClient::from_config(&config, &graph_name) else {
            eprintln!("skipping live FalkorDB read test: could not connect");
            return;
        };
        let rows = client
            .with_sync_graph(|graph| {
                let result = graph.ro_query("RETURN 1 AS value").execute()?;
                Ok(parse_falkor_result(result))
            })
            .expect("read through SyncGraph");

        assert_eq!(
            rows.first()
                .and_then(|row| row.get("value"))
                .and_then(|value| value.as_i64()),
            Some(1)
        );
    }

    fn live_falkor_fixture() -> Option<(FalkorConfig, String)> {
        let host = std::env::var("GOBBY_FALKORDB_HOST").ok()?;
        let port = std::env::var("GOBBY_FALKORDB_PORT")
            .ok()
            .and_then(|value| value.parse::<u16>().ok())
            .unwrap_or(16379);
        let password = std::env::var("GOBBY_FALKORDB_PASSWORD")
            .ok()
            .filter(|value| !value.is_empty());
        let graph_name = std::env::var("GOBBY_FALKORDB_TEST_GRAPH")
            .unwrap_or_else(|_| "gobby_core_live_test".to_string());
        Some((
            FalkorConfig {
                host,
                port,
                password,
            },
            graph_name,
        ))
    }
}
