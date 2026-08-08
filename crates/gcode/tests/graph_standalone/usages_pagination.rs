use super::support::*;

// UUIDv5(CODE_INDEX_UUID_NAMESPACE, <legacy label>) like the other fixtures.
const USAGES_PAGING_PROJECT_ID: &str = "e12aa4c7-9ec4-5e1f-9ad8-00dc21db2ffe"; // graph-standalone-usages-paging
const USAGES_PAGING_CALLER_ID: &str = "5023d255-797d-553c-a76c-4fc04e7da1cc"; // graph-standalone-usages-paging-caller
const USAGES_PAGING_CALLEE_ID: &str = "88891d82-fa5c-570f-ba94-47fe64deeea7"; // graph-standalone-usages-paging-callee
const USAGES_PAGING_FILE_ID: &str = "c0e15ee9-c150-5838-9ec6-d62495354c8f"; // graph-standalone-usages-paging-file

/// One raw graph page is MAX_GRAPH_LIMIT (100) rows, so 102 tied edges force
/// `find_usages` to stitch two separate Cypher executions together.
const TIED_EDGE_COUNT: usize = 102;

fn clear_seeded_graph(graph: &mut GraphClient) {
    let params = string_params(&[("project", USAGES_PAGING_PROJECT_ID)]);
    graph
        .query(
            "MATCH (n {project: $project}) DETACH DELETE n",
            Some(params),
        )
        .expect("clear seeded usages-paging graph");
}

fn source_lines(results: &Value) -> Vec<u64> {
    results
        .as_array()
        .unwrap_or_else(|| panic!("results must be an array: {results}"))
        .iter()
        .map(|row| {
            row["metadata"]["source_line"]
                .as_u64()
                .unwrap_or_else(|| panic!("usage row must carry metadata.source_line: {row}"))
        })
        .collect()
}

/// Tied rows (same source symbol, edge line, and edge file) spanning the raw
/// graph page boundary must paginate without duplicates or gaps: the query
/// orders by internal edge id as the final unique tiebreaker.
#[test]
fn usages_pagination_is_deterministic_across_tied_rows() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping usages pagination; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src")).expect("create src");
    fs::write(
        project.path().join("src/lib.rs"),
        "pub fn pager_caller() { pager_callee(); }\npub fn pager_callee() {}\n",
    )
    .expect("write source");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": USAGES_PAGING_PROJECT_ID,
            "name": "graph-standalone-usages-paging",
            "created_at": "2026-08-08T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, USAGES_PAGING_PROJECT_ID);
    cleanup_project(&mut conn, USAGES_PAGING_PROJECT_ID).expect("cleanup prior rows");
    conn.batch_execute(&format!(
        "INSERT INTO code_indexed_projects (id)
         VALUES ('{USAGES_PAGING_PROJECT_ID}');

         INSERT INTO code_indexed_project_states
            (machine_id, project_id, root_path, total_files, total_symbols, last_indexed_at,
             index_duration_ms)
         VALUES
            ('{HARNESS_MACHINE_ID}', '{USAGES_PAGING_PROJECT_ID}',
             '/tmp/graph-standalone-usages-paging', 1, 2, NOW(), 0);

         INSERT INTO code_indexed_files
            (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
             graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
         VALUES
            ('{USAGES_PAGING_FILE_ID}', '{USAGES_PAGING_PROJECT_ID}', 'src/lib.rs', 'rust',
             'hash-paging', 2, 66, true, true, NOW(), NOW());

         INSERT INTO code_indexed_file_states
            (machine_id, project_id, file_path, content_hash)
         VALUES
            ('{HARNESS_MACHINE_ID}', '{USAGES_PAGING_PROJECT_ID}', 'src/lib.rs',
             'hash-paging');

         INSERT INTO code_symbols
            (id, project_id, file_path, name, qualified_name, kind, language, byte_start, byte_end,
             line_start, line_end, signature, docstring, parent_symbol_id, file_content_hash,
             content_hash, summary, created_at, updated_at)
         VALUES
            ('{USAGES_PAGING_CALLER_ID}', '{USAGES_PAGING_PROJECT_ID}', 'src/lib.rs',
             'pager_caller', 'crate::pager_caller', 'function', 'rust', 0, 41, 1, 1,
             'pub fn pager_caller()', NULL, NULL, 'hash-paging', 'hash-caller', NULL, NOW(),
             NOW()),
            ('{USAGES_PAGING_CALLEE_ID}', '{USAGES_PAGING_PROJECT_ID}', 'src/lib.rs',
             'pager_callee', 'crate::pager_callee', 'function', 'rust', 42, 65, 2, 2,
             'pub fn pager_callee()', NULL, NULL, 'hash-paging', 'hash-callee', NULL, NOW(),
             NOW());"
    ))
    .expect("seed usages paging rows");

    let mut graph = phantom_graph_client(&env);
    clear_seeded_graph(&mut graph);
    // Every edge ties on (source.id, r.line, r.file); source_line is the only
    // per-edge distinguisher surfaced in output metadata.
    graph
        .query(
            &format!(
                "CREATE (src:CodeSymbol {{id: '{USAGES_PAGING_CALLER_ID}', \
                         project: '{USAGES_PAGING_PROJECT_ID}', name: 'pager_caller', \
                         file_path: 'src/lib.rs', line_start: 1, kind: 'function'}}), \
                        (tgt:CodeSymbol {{id: '{USAGES_PAGING_CALLEE_ID}', \
                         project: '{USAGES_PAGING_PROJECT_ID}', name: 'pager_callee', \
                         file_path: 'src/lib.rs', line_start: 2, kind: 'function'}}) \
                 WITH src, tgt \
                 UNWIND range(1, {TIED_EDGE_COUNT}) AS i \
                 CREATE (src)-[:CALLS {{file: 'src/lib.rs', line: 1, \
                         content_hash: 'hash-' + toString(i), provenance: 'EXTRACTED', \
                         source_system: 'test', source_line: i}}]->(tgt)"
            ),
            None,
        )
        .expect("seed tied CALLS edges");

    let expected: Vec<u64> = (1..=TIED_EDGE_COUNT as u64).collect();

    // Single call spanning the internal raw page boundary at 100 rows.
    let full = json_command(
        &env,
        project.path(),
        &[
            "usages",
            "pager_callee",
            "--limit",
            &TIED_EDGE_COUNT.to_string(),
        ],
    );
    let mut full_lines = source_lines(&full["results"]);
    full_lines.sort_unstable();
    assert_eq!(
        full_lines, expected,
        "full page must return every tied edge exactly once: {full}"
    );

    // Two user-level pages from separate invocations must not overlap or gap.
    let half = TIED_EDGE_COUNT / 2;
    let first = json_command(
        &env,
        project.path(),
        &["usages", "pager_callee", "--limit", &half.to_string()],
    );
    let second = json_command(
        &env,
        project.path(),
        &[
            "usages",
            "pager_callee",
            "--limit",
            &half.to_string(),
            "--offset",
            &half.to_string(),
        ],
    );
    let mut paged_lines = source_lines(&first["results"]);
    paged_lines.extend(source_lines(&second["results"]));
    paged_lines.sort_unstable();
    assert_eq!(
        paged_lines, expected,
        "consecutive pages must partition the tied edges\nfirst: {first}\nsecond: {second}"
    );

    clear_seeded_graph(&mut graph);
    cleanup_project(&mut conn, USAGES_PAGING_PROJECT_ID).expect("cleanup seeded rows");
}
