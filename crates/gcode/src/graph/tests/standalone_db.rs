use crate::integration_test_support::{ProjectCleanup, cleanup_project};
use postgres::{Client, NoTls};

const HARNESS_MACHINE_ID: &str = "ff5dd0ce-20a1-5f6c-8a89-ea85c2bbeea9";
const CPP_DB_LOCAL_PROJECT_ID: &str = "636a88e5-5cb1-5f3d-91e0-29c1af3d1a71";

fn standalone_database_url() -> Option<String> {
    let database_url = std::env::var("GCODE_GRAPH_STANDALONE_DATABASE_URL").ok()?;
    std::env::var("GCODE_GRAPH_STANDALONE_FALKOR_HOST").ok()?;
    std::env::var("GCODE_GRAPH_STANDALONE_FALKOR_PORT").ok()?;
    Some(database_url)
}

#[test]
fn db_resolves_seeded_cpp_candidate_file_to_symbol_id() {
    let Some(database_url) = standalone_database_url() else {
        eprintln!(
            "skipping seeded C++ local-callee DB resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let mut conn = Client::connect(&database_url, NoTls).expect("connect PostgreSQL");
    cleanup_project(&mut conn, CPP_DB_LOCAL_PROJECT_ID).expect("cleanup C++ DB project");
    let _cleanup = ProjectCleanup::new(&database_url, CPP_DB_LOCAL_PROJECT_ID);
    // UUIDv5(CODE_INDEX_UUID_NAMESPACE, <legacy label>) fixture ids.
    const CPP_DB_HELPER_FILE_ID: &str = "e6e16be2-5a6a-5bb6-b050-d1c2b02b7af0"; // graph-standalone-cpp-db-helper-file
    const CPP_DB_HELPER_ID: &str = "d9a664f0-2a4f-50d6-98d2-8ae87aaefc71"; // graph-standalone-cpp-db-helper
    conn.batch_execute(&format!(
        "INSERT INTO code_indexed_projects (id)
         VALUES ('{CPP_DB_LOCAL_PROJECT_ID}');

         INSERT INTO code_indexed_project_states
            (machine_id, project_id, root_path, total_files, total_symbols, last_indexed_at,
             index_duration_ms)
         VALUES
            ('{HARNESS_MACHINE_ID}', '{CPP_DB_LOCAL_PROJECT_ID}',
             '/tmp/graph-standalone-cpp-db', 1, 1, NOW(), 0);

         INSERT INTO code_indexed_files
            (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
             graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
         VALUES
            ('{CPP_DB_HELPER_FILE_ID}', '{CPP_DB_LOCAL_PROJECT_ID}',
             'include/helper.hpp', 'cpp', 'hash-cpp-helper', 1, 53, false, true, NULL, NOW());

         INSERT INTO code_indexed_file_states
            (machine_id, project_id, file_path, content_hash)
         VALUES
            ('{HARNESS_MACHINE_ID}', '{CPP_DB_LOCAL_PROJECT_ID}', 'include/helper.hpp',
             'hash-cpp-helper');

         INSERT INTO code_symbols
            (id, project_id, file_path, name, qualified_name, kind, language, byte_start, byte_end,
             line_start, line_end, signature, docstring, parent_symbol_id, file_content_hash,
             content_hash, summary, created_at, updated_at)
         VALUES
            ('{CPP_DB_HELPER_ID}', '{CPP_DB_LOCAL_PROJECT_ID}',
             'include/helper.hpp', 'helper', 'helper', 'function', 'cpp', 14, 52,
             3, 5, 'inline int helper()', NULL, NULL, 'hash-cpp-helper', 'hash-sym-cpp-helper',
             NULL, NOW(), NOW());"
    ))
    .expect("seed C++ DB rows");

    let target_files = vec!["include/helper.hpp".to_string()];
    let resolved = crate::db::resolve_local_callee_symbol_id(
        &mut conn,
        CPP_DB_LOCAL_PROJECT_ID,
        &target_files,
        "helper",
    )
    .expect("resolve seeded C++ local callee");
    assert_eq!(
        resolved.as_deref(),
        Some(CPP_DB_HELPER_ID),
        "seeded C++ candidate file should resolve to the indexed symbol id"
    );
}
