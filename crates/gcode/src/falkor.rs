//! FalkorDB read client for graph queries.
//!
//! Read helpers degrade gracefully through `with_falkor`: missing config,
//! connection construction failure, and query failures return caller-provided
//! defaults so search and graph commands remain usable without FalkorDB.

use std::collections::HashMap;

use falkordb::{
    FalkorClientBuilder, FalkorConnectionInfo, FalkorValue, LazyResultSet, QueryResult, SyncGraph,
};
use serde_json::{Map, Number, Value};

use crate::config::{Context, FalkorConfig};

/// Row from a FalkorDB query response.
pub type Row = HashMap<String, Value>;

/// Blocking FalkorDB graph client.
pub struct FalkorClient {
    graph: SyncGraph,
}

impl FalkorClient {
    pub fn from_config(config: &FalkorConfig) -> anyhow::Result<Self> {
        let password = config.password.as_deref().unwrap_or_default();
        let url = format!(
            "falkor://:{}@{}:{}",
            urlencoding::encode(password),
            config.host,
            config.port
        );
        let conn_info: FalkorConnectionInfo = url.as_str().try_into()?;
        let client = FalkorClientBuilder::new()
            .with_connection_info(conn_info)
            .build()?;
        Ok(Self {
            graph: client.select_graph(&config.graph_name),
        })
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
}

pub fn cypher_string_literal(s: &str) -> String {
    let escaped = s.replace('\\', "\\\\").replace('\'', "\\'");
    format!("'{escaped}'")
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

pub fn with_falkor<T>(
    ctx: &Context,
    default: T,
    f: impl FnOnce(&mut FalkorClient) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    let Some(config) = &ctx.falkordb else {
        return Ok(default);
    };

    let mut client = match FalkorClient::from_config(config) {
        Ok(client) => client,
        Err(e) => {
            if !ctx.quiet {
                eprintln!("Warning: FalkorDB connection failed: {e}");
            }
            return Ok(default);
        }
    };

    match f(&mut client) {
        Ok(value) => Ok(value),
        Err(e) => {
            if !ctx.quiet {
                eprintln!("Warning: FalkorDB query failed: {e}");
            }
            Ok(default)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use falkordb::FalkorValue;
    use serde_json::json;

    fn assert_no_numeric_or_list_placeholders(query: &str) {
        assert!(!query.contains("$offset"), "{query}");
        assert!(!query.contains("$limit"), "{query}");
        assert!(!query.contains("$ids"), "{query}");
    }

    #[test]
    fn cypher_string_literal_escapes_single_quotes_and_backslashes() {
        assert_eq!(
            cypher_string_literal("module\\path'symbol"),
            "'module\\\\path\\'symbol'"
        );
    }

    #[test]
    fn find_callers_query_interpolates_numeric_skip_and_limit() {
        let (query, params) = find_callers_query("project-1", "symbol-1", 17, 0);

        assert!(query.contains("SKIP 17 LIMIT 1"), "{query}");
        assert_no_numeric_or_list_placeholders(&query);
        assert_eq!(params.get("project").map(String::as_str), Some("'project-1'"));
        assert_eq!(params.get("id").map(String::as_str), Some("'symbol-1'"));
    }

    #[test]
    fn batch_query_uses_one_interpolated_in_list() {
        let (query, params) = find_callers_batch_query(
            "project-1",
            &["a".to_string(), "b'\\c".to_string()],
            250,
        );

        assert_eq!(query.matches(" IN [").count(), 1, "{query}");
        assert!(query.contains("target.id IN ['a', 'b\\'\\\\c']"), "{query}");
        assert!(query.contains("LIMIT 100"), "{query}");
        assert_no_numeric_or_list_placeholders(&query);
        assert_eq!(params.get("project").map(String::as_str), Some("'project-1'"));
    }

    #[test]
    fn blast_radius_query_clamps_depth_and_interpolates_limit() {
        let query = blast_radius_query(99, 250);

        assert!(query.contains(CALL_TARGET_PREDICATE), "{query}");
        assert!(query.contains("[:CALLS*1..5]"), "{query}");
        assert!(query.contains("LIMIT 100"), "{query}");
        assert_no_numeric_or_list_placeholders(&query);
    }

    #[test]
    fn convert_falkor_records_maps_headers_and_row_values() {
        let headers = vec!["name".to_string(), "age".to_string(), "empty".to_string()];
        let rows = vec![vec![
            FalkorValue::String("Alice".to_string()),
            FalkorValue::I64(30),
            FalkorValue::None,
        ]];

        let parsed = parse_falkor_records(headers, rows);

        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].get("name"), Some(&json!("Alice")));
        assert_eq!(parsed[0].get("age"), Some(&json!(30)));
        assert_eq!(parsed[0].get("empty"), Some(&json!(null)));
    }

    #[test]
    fn row_to_graph_result_prefers_blast_radius_node_fields() {
        let row = Row::from([
            ("node_id".to_string(), json!("sym-1")),
            ("node_name".to_string(), json!("foo")),
            ("file_path".to_string(), json!("src/main.py")),
            ("line".to_string(), json!(42)),
            ("rel_type".to_string(), json!("call")),
            ("distance".to_string(), json!(2)),
        ]);

        let result = row_to_graph_result(&row);

        assert_eq!(result.id, "sym-1");
        assert_eq!(result.name, "foo");
        assert_eq!(result.file_path, "src/main.py");
        assert_eq!(result.line, 42);
        assert_eq!(result.relation.as_deref(), Some("call"));
        assert_eq!(result.distance, Some(2));
    }
}
