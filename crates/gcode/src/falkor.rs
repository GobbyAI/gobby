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
