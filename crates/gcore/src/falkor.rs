//! FalkorDB foundation adapter boundary.
//!
//! This module is available with the `falkor` feature. The feature enables the
//! direct Redis client used to execute FalkorDB `GRAPH.QUERY` commands with
//! socket-level timeouts.
//! Duplicate-index suppression is based on observed FalkorDB/driver message
//! fragments because FalkorDB does not expose a stable typed duplicate-index
//! error through Redis. Live tests are env-gated against the caller-provided
//! service image; this adapter intentionally does not claim a tested FalkorDB
//! version range.

use std::collections::HashMap;
use std::io::ErrorKind;
use std::thread;
use std::time::Duration;

use redis::Value as RedisValue;
use redis::{Client, Cmd, Connection, ConnectionAddr, ConnectionInfo, RedisConnectionInfo};
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
    connection: Connection,
    graph_name: String,
    query_timeout: Duration,
}

/// Handshake and socket I/O share the same bound so AUTH can wait out an
/// in-flight `GRAPH.QUERY` on single-threaded Redis. Connection refused still
/// fails immediately; a busy server no longer AUTH-times-out at 5s.
const DEFAULT_CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const DEFAULT_SOCKET_TIMEOUT: Duration = Duration::from_secs(30);
const HANDSHAKE_ATTEMPTS: usize = 3;
const HANDSHAKE_RETRY_BACKOFF: [Duration; 2] =
    [Duration::from_millis(100), Duration::from_millis(200)];

impl GraphClient {
    /// Build a client for a consumer-selected graph.
    pub fn from_config(config: &FalkorConfig, graph_name: &str) -> anyhow::Result<Self> {
        Self::from_config_with_timeouts(
            config,
            graph_name,
            DEFAULT_CONNECT_TIMEOUT,
            DEFAULT_SOCKET_TIMEOUT,
        )
    }

    /// Build a client with explicit timeouts. Intended for tests and internal
    /// callers that need a shorter bound than the production defaults.
    pub fn from_config_with_timeouts(
        config: &FalkorConfig,
        graph_name: &str,
        connect_timeout: Duration,
        socket_timeout: Duration,
    ) -> anyhow::Result<Self> {
        let connection_info = ConnectionInfo {
            addr: ConnectionAddr::Tcp(config.host.clone(), config.port),
            redis: RedisConnectionInfo {
                db: 0,
                username: None,
                password: config
                    .password
                    .clone()
                    .filter(|password| !password.is_empty()),
                ..RedisConnectionInfo::default()
            },
        };
        let client = Client::open(connection_info)?;
        let connection = client.get_connection_with_timeout(connect_timeout)?;
        connection.set_read_timeout(Some(socket_timeout))?;
        connection.set_write_timeout(Some(socket_timeout))?;

        Ok(Self {
            connection,
            graph_name: graph_name.to_string(),
            query_timeout: query_timeout_from_socket(socket_timeout),
        })
    }

    /// Execute a Cypher query and return parsed rows.
    pub fn query(
        &mut self,
        cypher: &str,
        params: Option<HashMap<String, String>>,
    ) -> anyhow::Result<Vec<Row>> {
        let query = construct_query(cypher, params.as_ref());
        let response = graph_query_cmd(&self.graph_name, &query, self.query_timeout)
            .query::<RedisValue>(&mut self.connection)?;
        parse_compact_response(response)
    }

    /// Ensure an exact node index exists for a label/property pair.
    pub fn ensure_exact_node_index(&mut self, label: &str, property: &str) -> anyhow::Result<()> {
        let cypher = format!(
            "CREATE INDEX ON :{}({})",
            escape_label(label),
            escape_property(property)
        );
        self.ensure_index(&cypher, &format!(":{label}({property})"))
    }

    /// Ensure an exact relationship index exists for a type/property pair.
    pub fn ensure_exact_relationship_index(
        &mut self,
        rel_type: &str,
        property: &str,
    ) -> anyhow::Result<()> {
        let cypher = format!(
            "CREATE INDEX FOR ()-[r:{}]-() ON (r.{})",
            escape_rel_type(rel_type),
            escape_property(property)
        );
        self.ensure_index(&cypher, &format!("()-[:{rel_type}]->().{property}"))
    }

    fn ensure_index(&mut self, cypher: &str, index_name: &str) -> anyhow::Result<()> {
        match self.query(cypher, None) {
            Ok(_) => Ok(()),
            Err(error) if is_existing_index_error(&error) => {
                log::debug!(
                    "FalkorDB index {index_name} already exists; suppressing duplicate-index error: {error}"
                );
                Ok(())
            }
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
    mut make_client: impl FnMut(&FalkorConfig, &str) -> anyhow::Result<C>,
    f: impl FnOnce(&mut C) -> anyhow::Result<T>,
) -> anyhow::Result<(T, ServiceState)> {
    let Some(config) = config else {
        log::trace!("FalkorDB graph `{graph_name}` unavailable: missing config");
        return Ok((default, ServiceState::NotConfigured));
    };

    for attempt in 1..=HANDSHAKE_ATTEMPTS {
        match make_client(config, graph_name) {
            Ok(mut client) => {
                let value = f(&mut client)?;
                return Ok((value, ServiceState::Available));
            }
            Err(error) if is_transient_graph_io(&error) && attempt < HANDSHAKE_ATTEMPTS => {
                log::debug!(
                    "FalkorDB graph `{graph_name}` handshake attempt {attempt}/{HANDSHAKE_ATTEMPTS} \
                     transient: {error}"
                );
                thread::sleep(HANDSHAKE_RETRY_BACKOFF[attempt - 1]);
            }
            Err(error) => {
                log::debug!("FalkorDB graph `{graph_name}` unavailable: {error}");
                return Ok((
                    default,
                    ServiceState::Unreachable {
                        message: error.to_string(),
                    },
                ));
            }
        }
    }

    unreachable!("handshake retry loop returns on the final attempt");
}

fn query_timeout_from_socket(socket_timeout: Duration) -> Duration {
    socket_timeout
        .checked_sub(Duration::from_secs(1))
        .filter(|timeout| *timeout > Duration::ZERO)
        .unwrap_or(Duration::from_millis(1))
}

fn graph_query_cmd(graph_name: &str, query: &str, timeout: Duration) -> Cmd {
    let timeout_ms = u64::try_from(timeout.as_millis())
        .unwrap_or(u64::MAX)
        .max(1);
    let mut cmd = redis::cmd("GRAPH.QUERY");
    cmd.arg(graph_name)
        .arg(query)
        .arg("--compact")
        .arg("timeout")
        .arg(timeout_ms);
    cmd
}

fn is_transient_graph_io(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        if let Some(io_error) = cause.downcast_ref::<std::io::Error>() {
            return matches!(
                io_error.kind(),
                ErrorKind::TimedOut | ErrorKind::WouldBlock | ErrorKind::Interrupted
            );
        }
        let lowered = cause.to_string().to_ascii_lowercase();
        lowered.contains("os error 35")
            || lowered.contains("resource temporarily unavailable")
            || lowered.contains("would block")
    })
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
///
/// The quoting is load-bearing: falkordb 0.2 interpolates params as raw
/// `CYPHER k=v` text, so an unquoted value is a syntax error or an injection
/// vector. Removing this once broke every gwiki graph query (#670) — keep the
/// quotes even though typed param APIs elsewhere don't need them.
pub fn escape_string(value: &str) -> String {
    let escaped = value.replace('\\', "\\\\").replace('\'', "\\'");
    format!("'{escaped}'")
}

fn escape_identifier(value: &str) -> String {
    format!("`{}`", value.replace('`', "``"))
}

const EXISTING_INDEX_ERROR_PATTERNS: &[&str] =
    &["already indexed", "already exists", "index already exists"];

fn is_existing_index_error(error: &anyhow::Error) -> bool {
    let message = error.to_string().to_ascii_lowercase();
    // FalkorDB currently reports duplicate-index creation through version- and
    // driver-specific message strings instead of a stable typed error code.
    // TODO: replace these exact message patterns if the driver exposes a typed
    // duplicate-index error.
    let matched = EXISTING_INDEX_ERROR_PATTERNS
        .iter()
        .any(|pattern| message.contains(pattern));
    if !matched && message.contains("index") {
        log::debug!("unmatched FalkorDB index-like error: {error}");
    }
    matched
}

fn construct_query(query_str: &str, params: Option<&HashMap<String, String>>) -> String {
    let params = params
        .map(|params| {
            params
                .iter()
                .map(|(key, value)| format!("{key}={value}"))
                .collect::<Vec<_>>()
                .join(" ")
        })
        .filter(|params| !params.is_empty())
        .map(|params| format!("CYPHER {params} "))
        .unwrap_or_default();
    format!("{params}{query_str}")
}

fn parse_compact_response(response: RedisValue) -> anyhow::Result<Vec<Row>> {
    if let RedisValue::ServerError(error) = response {
        anyhow::bail!("FalkorDB server error: {error:?}");
    }

    let sections = redis_array(response)?;
    match sections.as_slice() {
        [stats] => {
            parse_stats(stats.clone())?;
            Ok(Vec::new())
        }
        [headers, stats] => {
            parse_header(headers.clone())?;
            parse_stats(stats.clone())?;
            Ok(Vec::new())
        }
        [headers, data, stats] => {
            let headers = parse_header(headers.clone())?;
            parse_stats(stats.clone())?;
            parse_compact_records(headers, data.clone())
        }
        _ => anyhow::bail!(
            "invalid FalkorDB response: expected stats, header+stats, or header+data+stats"
        ),
    }
}

fn parse_header(header: RedisValue) -> anyhow::Result<Vec<String>> {
    redis_array(header)?
        .into_iter()
        .map(|item| {
            let mut item = redis_array(item)?;
            let key = if item.len() == 2 {
                item.remove(1)
            } else {
                item.into_iter()
                    .next()
                    .ok_or_else(|| anyhow::anyhow!("invalid FalkorDB header: empty item"))?
            };
            redis_string(key)
        })
        .collect()
}

fn parse_stats(stats: RedisValue) -> anyhow::Result<Vec<String>> {
    redis_array(stats)?.into_iter().map(redis_string).collect()
}

fn parse_compact_records(headers: Vec<String>, data: RedisValue) -> anyhow::Result<Vec<Row>> {
    redis_array(data)?
        .into_iter()
        .map(|record| {
            let fields = redis_array(record)?;
            let mut row = HashMap::new();
            for (index, field) in headers.iter().enumerate() {
                let value = fields.get(index).cloned().unwrap_or(RedisValue::Nil);
                row.insert(field.clone(), compact_value_to_json(value)?);
            }
            Ok(row)
        })
        .collect()
}

fn compact_value_to_json(value: RedisValue) -> anyhow::Result<Value> {
    let (marker, value) = type_value(value)?;
    Ok(match marker {
        1 => Value::Null,
        2 => Value::String(redis_string(value)?),
        3 => Value::Number(Number::from(redis_i64(value)?)),
        4 => Value::Bool(redis_bool(value)?),
        5 => Number::from_f64(redis_f64(value)?)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        6 => Value::Array(
            redis_array(value)?
                .into_iter()
                .map(compact_value_to_json)
                .collect::<anyhow::Result<Vec<_>>>()?,
        ),
        10 => Value::Object(parse_map(value)?),
        7 => unsupported_marker("edge", marker, value),
        8 => unsupported_marker("node", marker, value),
        9 => unsupported_marker("path", marker, value),
        11 => unsupported_marker("point", marker, value),
        12 => unsupported_marker("vec32", marker, value),
        _ => unsupported_marker("unknown", marker, value),
    })
}

fn unsupported_marker(kind: &str, marker: i64, value: RedisValue) -> Value {
    Value::String(format!(
        "unsupported FalkorDB graph value marker {marker} ({kind}): {value:?}"
    ))
}

fn parse_map(value: RedisValue) -> anyhow::Result<Map<String, Value>> {
    let entries = match value {
        RedisValue::Map(entries) => entries,
        RedisValue::Array(values) => {
            if values.len() % 2 != 0 {
                anyhow::bail!("invalid FalkorDB map: odd number of array entries");
            }
            values
                .as_chunks::<2>()
                .0
                .iter()
                .map(|[key, value]| (key.clone(), value.clone()))
                .collect()
        }
        value => anyhow::bail!("invalid FalkorDB map value: {value:?}"),
    };

    entries
        .into_iter()
        .map(|(key, value)| Ok((redis_string(key)?, compact_value_to_json(value)?)))
        .collect()
}

fn type_value(value: RedisValue) -> anyhow::Result<(i64, RedisValue)> {
    if matches!(value, RedisValue::Nil) {
        return Ok((1, RedisValue::Nil));
    }

    let values = redis_array(value)?;
    let [marker, value]: [RedisValue; 2] = values
        .try_into()
        .map_err(|_| anyhow::anyhow!("invalid FalkorDB typed value"))?;
    Ok((redis_i64(marker)?, value))
}

fn redis_array(value: RedisValue) -> anyhow::Result<Vec<RedisValue>> {
    match value {
        RedisValue::Array(values) => Ok(values),
        value => anyhow::bail!("expected Redis array, got {value:?}"),
    }
}

fn redis_string(value: RedisValue) -> anyhow::Result<String> {
    match value {
        RedisValue::BulkString(value) => String::from_utf8(value).map_err(Into::into),
        RedisValue::SimpleString(value) => Ok(value),
        RedisValue::VerbatimString { text, .. } => Ok(text),
        RedisValue::Okay => Ok("OK".to_string()),
        value => anyhow::bail!("expected Redis string, got {value:?}"),
    }
}

fn redis_i64(value: RedisValue) -> anyhow::Result<i64> {
    match value {
        RedisValue::Int(value) => Ok(value),
        value => anyhow::bail!("expected Redis integer, got {value:?}"),
    }
}

fn redis_bool(value: RedisValue) -> anyhow::Result<bool> {
    match value {
        RedisValue::Boolean(value) => Ok(value),
        RedisValue::BulkString(value) => match String::from_utf8(value)?.as_str() {
            "true" => Ok(true),
            "false" => Ok(false),
            value => anyhow::bail!("expected FalkorDB boolean, got {value:?}"),
        },
        RedisValue::SimpleString(value) => match value.as_str() {
            "true" => Ok(true),
            "false" => Ok(false),
            value => anyhow::bail!("expected FalkorDB boolean, got {value:?}"),
        },
        value => anyhow::bail!("expected FalkorDB boolean, got {value:?}"),
    }
}

fn redis_f64(value: RedisValue) -> anyhow::Result<f64> {
    match value {
        RedisValue::Double(value) => Ok(value),
        RedisValue::Int(value) => Ok(value as f64),
        RedisValue::BulkString(value) => Ok(String::from_utf8(value)?.parse()?),
        RedisValue::SimpleString(value) => Ok(value.parse()?),
        value => anyhow::bail!("expected FalkorDB float, got {value:?}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::FalkorConfig;
    use crate::degradation::ServiceState;
    use anyhow::anyhow;
    use serde_json::json;
    use std::io::{ErrorKind, Read};
    use std::net::TcpListener;
    use std::sync::mpsc;
    use std::thread;
    use std::time::Instant;

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
    fn compact_response_parses_scalar_rows() {
        let rows = parse_compact_response(RedisValue::Array(vec![
            header(&["name", "count", "ratio", "flag", "missing"]),
            RedisValue::Array(vec![RedisValue::Array(vec![
                typed(2, bulk("alpha")),
                typed(3, RedisValue::Int(42)),
                typed(5, bulk("1.5")),
                typed(4, bulk("true")),
            ])]),
            stats(),
        ]))
        .expect("scalar compact response should parse");

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].get("name"), Some(&json!("alpha")));
        assert_eq!(rows[0].get("count"), Some(&json!(42)));
        assert_eq!(rows[0].get("ratio"), Some(&json!(1.5)));
        assert_eq!(rows[0].get("flag"), Some(&json!(true)));
        assert_eq!(rows[0].get("missing"), Some(&Value::Null));
    }

    #[test]
    fn compact_response_parses_empty_stat_only_writes() {
        let stat_only = parse_compact_response(RedisValue::Array(vec![stats()]))
            .expect("stat-only response should parse");
        assert!(stat_only.is_empty());

        let header_only =
            parse_compact_response(RedisValue::Array(vec![header(&["value"]), stats()]))
                .expect("header+stats response should parse");
        assert!(header_only.is_empty());
    }

    #[test]
    fn compact_response_parses_arrays_and_maps() {
        let nested = typed(
            10,
            RedisValue::Array(vec![
                bulk("name"),
                typed(2, bulk("inner")),
                bulk("values"),
                typed(
                    6,
                    RedisValue::Array(vec![
                        typed(3, RedisValue::Int(7)),
                        typed(1, RedisValue::Nil),
                    ]),
                ),
            ]),
        );
        let rows = parse_compact_response(RedisValue::Array(vec![
            header(&["payload"]),
            RedisValue::Array(vec![RedisValue::Array(vec![nested])]),
            stats(),
        ]))
        .expect("nested compact response should parse");

        assert_eq!(
            rows[0].get("payload"),
            Some(&json!({
                "name": "inner",
                "values": [7, null]
            }))
        );
    }

    #[test]
    fn compact_response_returns_server_errors() {
        let response =
            redis::parse_redis_value(b"-ERR syntax error near RETURN\r\n").expect("RESP error");
        let error = parse_compact_response(response).expect_err("server error should fail");

        assert!(error.to_string().contains("syntax error near RETURN"));
    }

    #[test]
    fn compact_response_returns_diagnostics_for_unsupported_markers() {
        let rows = parse_compact_response(RedisValue::Array(vec![
            header(&["node"]),
            RedisValue::Array(vec![RedisValue::Array(vec![typed(
                8,
                RedisValue::Array(vec![]),
            )])]),
            stats(),
        ]))
        .expect("unsupported markers should parse as diagnostics");

        let diagnostic = rows[0]
            .get("node")
            .and_then(Value::as_str)
            .expect("node marker should return diagnostic string");
        assert!(diagnostic.contains("unsupported FalkorDB graph value marker 8 (node)"));
    }

    #[test]
    fn nonresponsive_socket_setup_is_bounded_and_dropped() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind test listener");
        let port = listener.local_addr().expect("listener address").port();
        let (accepted_tx, accepted_rx) = mpsc::channel();
        let (closed_tx, closed_rx) = mpsc::channel();

        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept test connection");
            let _ = accepted_tx.send(());
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .expect("set listener read timeout");
            let mut buf = [0_u8; 1024];
            loop {
                match stream.read(&mut buf) {
                    Ok(0) => {
                        let _ = closed_tx.send(true);
                        break;
                    }
                    Ok(_) => continue,
                    Err(error)
                        if matches!(error.kind(), ErrorKind::TimedOut | ErrorKind::WouldBlock) =>
                    {
                        let _ = closed_tx.send(false);
                        break;
                    }
                    Err(_) => {
                        let _ = closed_tx.send(true);
                        break;
                    }
                }
            }
        });

        let config = FalkorConfig {
            host: "127.0.0.1".to_string(),
            port,
            password: None,
        };
        let started = Instant::now();
        let result = GraphClient::from_config_with_timeouts(
            &config,
            "timeout_test",
            Duration::from_millis(150),
            Duration::from_millis(150),
        );

        assert!(result.is_err(), "nonresponsive socket should fail");
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "nonresponsive socket should be bounded"
        );
        accepted_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("test server should accept the client connection");
        assert!(
            closed_rx
                .recv_timeout(Duration::from_secs(2))
                .expect("client socket should close after timeout"),
            "client connection should close after timeout"
        );
        handle.join().expect("listener thread should join");
    }

    fn bulk(value: &str) -> RedisValue {
        RedisValue::BulkString(value.as_bytes().to_vec())
    }

    fn typed(marker: i64, value: RedisValue) -> RedisValue {
        RedisValue::Array(vec![RedisValue::Int(marker), value])
    }

    fn header(names: &[&str]) -> RedisValue {
        RedisValue::Array(
            names
                .iter()
                .map(|name| RedisValue::Array(vec![RedisValue::Int(0), bulk(name)]))
                .collect(),
        )
    }

    fn stats() -> RedisValue {
        RedisValue::Array(vec![bulk(
            "Query internal execution time: 0.1 milliseconds",
        )])
    }

    #[test]
    fn live_graph_read_is_env_gated() {
        let Some((config, graph_name)) = live_falkor_fixture() else {
            eprintln!("skipping live FalkorDB read test: no managed grant fixture");
            return;
        };

        let Ok(mut client) = GraphClient::from_config(&config, &graph_name) else {
            eprintln!("skipping live FalkorDB read test: could not connect");
            return;
        };
        let rows = client
            .query("RETURN 1 AS value", None)
            .expect("read through GraphClient");

        assert_eq!(
            rows.first()
                .and_then(|row| row.get("value"))
                .and_then(|value| value.as_i64()),
            Some(1)
        );
    }

    fn live_falkor_fixture() -> Option<(FalkorConfig, String)> {
        let path = std::env::var("GOBBY_MANAGED_EXECUTION_BOOTSTRAP").ok()?;
        let grant = crate::grant::load_grant_file(std::path::Path::new(&path)).ok()?;
        let crate::grant::FalkorCapability::Direct {
            host,
            port,
            password,
        } = grant.capabilities.falkordb
        else {
            return None;
        };
        Some((
            FalkorConfig {
                host,
                port: u16::try_from(port).unwrap_or(16379),
                password: (!password.is_empty()).then_some(password),
            },
            "gobby_core_live_test".to_string(),
        ))
    }

    #[test]
    fn existing_index_errors_are_recognized_case_insensitively() {
        for message in [
            "Index already exists",
            "node property is already indexed",
            "ERR index already exists for label",
        ] {
            let error = anyhow::anyhow!(message);
            assert!(is_existing_index_error(&error), "{message}");
        }
    }

    #[test]
    fn unrelated_index_errors_are_not_suppressed() {
        let error = anyhow::anyhow!("syntax error near CREATE INDEX");

        assert!(!is_existing_index_error(&error));
    }

    #[test]
    fn handshake_retries_transient_io_then_succeeds() {
        let attempts = std::sync::atomic::AtomicUsize::new(0);
        let result = with_graph_client(
            Some(&test_config()),
            "consumer_graph",
            Vec::<String>::new(),
            |_config, _graph_name| {
                let n = attempts.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                if n < 2 {
                    Err(anyhow::Error::from(std::io::Error::from(
                        ErrorKind::WouldBlock,
                    )))
                } else {
                    Ok(FakeGraphClient)
                }
            },
            |_client| Ok(vec!["ok".to_string()]),
        )
        .expect("transient handshake should retry");

        assert_eq!(result, (vec!["ok".to_string()], ServiceState::Available));
        assert_eq!(attempts.load(std::sync::atomic::Ordering::SeqCst), 3);
    }

    #[test]
    fn handshake_does_not_retry_non_transient_errors() {
        let attempts = std::sync::atomic::AtomicUsize::new(0);
        let started = Instant::now();
        let result = with_graph_client(
            Some(&test_config()),
            "consumer_graph",
            Vec::<String>::new(),
            |_config, _graph_name| {
                attempts.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Err(anyhow!("NOAUTH: Authentication required"))
            },
            |_client: &mut FakeGraphClient| Ok(vec!["value".to_string()]),
        )
        .expect("non-transient failure should degrade");

        assert!(matches!(
            result,
            (_, ServiceState::Unreachable { ref message }) if message.contains("NOAUTH")
        ));
        assert_eq!(attempts.load(std::sync::atomic::Ordering::SeqCst), 1);
        assert!(started.elapsed() < Duration::from_millis(80));
    }

    #[test]
    fn graph_query_cmd_includes_timeout_below_socket_budget() {
        let packed = graph_query_cmd(
            "g",
            "RETURN 1",
            query_timeout_from_socket(Duration::from_secs(30)),
        )
        .get_packed_command();
        let text = String::from_utf8_lossy(&packed);
        assert!(text.contains("GRAPH.QUERY"), "{text}");
        assert!(text.contains("timeout"), "{text}");
        assert!(text.contains("29000"), "{text}");
    }

    #[test]
    fn query_timeout_floors_subsecond_socket_budget() {
        assert_eq!(
            query_timeout_from_socket(Duration::from_millis(150)),
            Duration::from_millis(1)
        );
    }

    #[test]
    fn transient_io_classification() {
        let blocked = anyhow::Error::from(std::io::Error::from(ErrorKind::WouldBlock));
        assert!(is_transient_graph_io(&blocked));
        let display = anyhow!("Resource temporarily unavailable (os error 35)");
        assert!(is_transient_graph_io(&display));
        let syntax = anyhow!("syntax error near RETURN");
        assert!(!is_transient_graph_io(&syntax));
    }
}
