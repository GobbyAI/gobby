use std::process::Command;

use serde_json::Value;

mod common;

#[test]
#[serial_test::serial(serial_db)]
fn test_evidence_cli_contract() -> anyhow::Result<()> {
    let output = Command::new(env!("CARGO_BIN_EXE_gcode"))
        .args(["evidence", "--request-json", "{"])
        .output()?;
    assert_error(&output, "invalid_evidence_request");

    #[cfg(gcode_postgres_tests)]
    database_contract()?;

    Ok(())
}

#[test]
fn snapshot_cli_returns_native_canonical_inventory_without_services() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let repo = temporary.path();
    Command::new("git")
        .args(["init", "--quiet", "-b", "main"])
        .current_dir(repo)
        .status()?
        .success()
        .then_some(())
        .ok_or_else(|| anyhow::anyhow!("git init failed"))?;
    std::fs::write(repo.join("choice.h"), "#define CHOICE 1\n")?;
    std::fs::write(repo.join("choice.cpp"), "int choice = 1;\n")?;
    Command::new("git")
        .args(["add", "."])
        .current_dir(repo)
        .status()?
        .success()
        .then_some(())
        .ok_or_else(|| anyhow::anyhow!("git add failed"))?;
    let commit = Command::new("git")
        .args([
            "-c",
            "user.name=Evidence Snapshot Test",
            "-c",
            "user.email=evidence-snapshot@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "root",
        ])
        .current_dir(repo)
        .status()?;
    anyhow::ensure!(commit.success(), "git commit failed");
    let commit_oid = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(repo)
            .output()?
            .stdout,
    )?
    .trim()
    .to_string();
    let request = serde_json::json!({
        "schema_version": 1,
        "project_id": "project-native-snapshot",
        "commit_oid": commit_oid,
        "action": "inspect",
    });
    let output = Command::new(env!("CARGO_BIN_EXE_gcode"))
        .args([
            "--quiet",
            "--format",
            "json",
            "--project",
            repo.to_str().expect("UTF-8 temporary path"),
            "evidence",
            "--snapshot-json",
            &serde_json::to_string(&request)?,
        ])
        .env_remove("DATABASE_URL")
        // Read-only inspection must not require a writable ambient temp directory.
        .env("TMPDIR", repo.join("unavailable-temp"))
        .env("TMP", repo.join("unavailable-temp"))
        .env("TEMP", repo.join("unavailable-temp"))
        .output()?;
    anyhow::ensure!(
        output.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );
    let response: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(response["schema_version"], 1);
    assert_eq!(response["binding"]["commit_oid"], request["commit_oid"]);
    assert_eq!(
        response["binding"]["commit"]["comparison_kind"],
        "empty_tree"
    );
    let header = response["inventory"]["entries"]
        .as_array()
        .expect("inventory entries")
        .iter()
        .find(|entry| entry["path"] == "choice.h")
        .expect("header entry");
    assert_eq!(header["language"], "c");
    Ok(())
}

fn assert_error(output: &std::process::Output, code: &str) -> Value {
    assert_eq!(
        output.status.code(),
        Some(2),
        "stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.stdout.is_empty(), "errors must not write stdout");
    let stderr = std::str::from_utf8(&output.stderr)
        .expect("error output is UTF-8")
        .trim();
    assert_eq!(stderr.lines().count(), 1, "typed error is one JSON line");
    let payload: Value = serde_json::from_str(stderr).expect("error output is JSON");
    assert_eq!(payload["error"], code);
    assert!(payload["message"].is_string());
    assert!(payload["recovery"].is_string());
    payload
}

#[cfg(gcode_postgres_tests)]
const PROJECT_ID: &str = "e62aef83-2575-53b9-830c-2e88a81d7a35";
#[cfg(gcode_postgres_tests)]
const FILE_PATH: &str = "src/lib.rs";
#[cfg(gcode_postgres_tests)]
const SOURCE: &str = concat!(
    "pub fn alpha() { let evidence_token = \"needle\"; }\n",
    "pub fn beta() { let evidence_token = \"needle\"; }\n",
);

#[cfg(gcode_postgres_tests)]
fn database_contract() -> anyhow::Result<()> {
    use gobby_code::evidence::{
        DEFAULT_GRAPH_DEPTH, DEFAULT_MAX_BYTES, DEFAULT_RESULT_LIMIT, EVIDENCE_SCHEMA_VERSION,
        EntitySelector, EvidenceItem, EvidenceOperation, EvidenceRequest, GraphQuery,
        GraphSelector, ReadSelector, SearchLane, SearchSelector, Snapshot,
    };
    use postgres::{Client, NoTls};

    let database_url =
        gobby_code::test_env::postgres_test_database_url("evidence CLI contract test");
    let mut conn = Client::connect(&database_url, NoTls)?;
    cleanup_project(&mut conn, PROJECT_ID)?;
    let cleanup = ProjectCleanup {
        database_url: database_url.clone(),
    };

    let project_dir = tempfile::tempdir()?;
    let project = project_dir.path().canonicalize()?;
    std::fs::create_dir_all(project.join(".gobby"))?;
    std::fs::create_dir_all(project.join("src"))?;
    std::fs::write(project.join(FILE_PATH), SOURCE)?;
    std::fs::write(
        project.join(".gobby/project.json"),
        serde_json::json!({"id": PROJECT_ID, "name": "evidence-cli-contract"}).to_string(),
    )?;
    git(&project, &["init", "--quiet", "-b", "main"])?;
    git(&project, &["add", FILE_PATH])?;
    let commit_oid = commit(&project, "initial evidence fixture")?;
    let snapshot = Snapshot::prepare(&project, PROJECT_ID, &commit_oid)?;
    assert_pure_preflight_rejections(snapshot.binding())?;

    gobby_code::test_env::seed_test_checkout(&mut conn, PROJECT_ID, &project)
        .map_err(anyhow::Error::msg)?;
    let home = isolated_gobby_home(&project)?;
    let connections = gobby_core::grant::DirectConnections::postgres(&database_url);
    let mut index = Command::new(env!("CARGO_BIN_EXE_gcode"));
    index
        .current_dir(&project)
        .args(["--quiet", "--project"])
        .arg(&project)
        .args(["index", "--full"]);
    attach_managed_grant(&mut index, &home, PROJECT_ID, &connections)?;
    let indexed = index.output()?;
    assert!(
        indexed.status.success(),
        "index fixture: {}",
        String::from_utf8_lossy(&indexed.stderr)
    );

    let status_before = git(&project, &["status", "--porcelain"])?;
    let facts_before = fact_count(&mut conn)?;
    let indexed_hash_before = indexed_hash(&mut conn)?;

    let range_request = EvidenceRequest {
        schema_version: EVIDENCE_SCHEMA_VERSION,
        binding: snapshot.binding().clone(),
        operation: EvidenceOperation::Read {
            read: ReadSelector::Range {
                path: FILE_PATH.to_string(),
                start_line: 1,
                end_line: 1,
            },
        },
        max_bytes: DEFAULT_MAX_BYTES,
        continuation: None,
    };
    let response = run_success(&project, &home, &connections, &range_request)?;
    let repeated = run_success(&project, &home, &connections, &range_request)?;
    assert_eq!(
        response, repeated,
        "identical reads are byte-contract deterministic"
    );
    assert_eq!(response.request, range_request);
    assert_eq!(response.binding, *snapshot.binding());
    assert_eq!(response.contract.name, "gcode-evidence");
    assert_eq!(response.contract.schema_version, EVIDENCE_SCHEMA_VERSION);
    assert_eq!(response.contract.tool, "gobby-code");
    assert_eq!(response.contract.lane, "read_range");
    assert!(response.complete);
    assert_eq!(response.bounds.returned_items, 1);
    assert_eq!(response.bounds.total_items, 1);
    assert!(response.exclusions.is_empty());
    assert!(response.warnings.is_empty());
    let EvidenceItem::Source(source) = &response.items[0] else {
        panic!("range response must contain source evidence");
    };
    let first_line = SOURCE
        .lines()
        .next()
        .expect("fixture first line")
        .to_string()
        + "\n";
    let entry = snapshot.entry(FILE_PATH)?;
    assert_eq!(source.path, FILE_PATH);
    assert_eq!(source.excerpt, first_line);
    assert_eq!(source.line_start, 1);
    assert_eq!(source.line_end, 1);
    assert_eq!(source.byte_start, 0);
    assert_eq!(source.byte_end, source.excerpt.len());
    assert_eq!(
        source.blob_oid,
        entry.blob_oid.as_deref().expect("blob oid")
    );
    assert_eq!(
        source.content_hash,
        entry.content_hash.as_deref().expect("content hash")
    );
    assert_eq!(
        source.excerpt_hash,
        gobby_core::indexing::content_hash(source.excerpt.as_bytes())
    );
    assert!(source.evidence_id.starts_with("src:"));

    let metadata_request = EvidenceRequest {
        operation: EvidenceOperation::Read {
            read: ReadSelector::CommitMetadata,
        },
        ..range_request.clone()
    };
    let metadata = run_success(&project, &home, &connections, &metadata_request)?;
    assert_eq!(metadata.contract.lane, "read_commit_metadata");
    assert_eq!(metadata.items.len(), 1);
    let EvidenceItem::CommitMetadata(record) = &metadata.items[0] else {
        panic!("metadata response must contain commit evidence");
    };
    assert_eq!(record.commit_oid, commit_oid);
    assert_eq!(record.parent_oids, snapshot.binding().commit.parent_oids);
    assert_eq!(record.changed_path_count, 1);
    assert_eq!(
        record
            .changed_path
            .as_ref()
            .and_then(|change| change.new_path.as_deref()),
        Some(FILE_PATH)
    );
    assert_eq!(
        record.changed_paths_digest,
        snapshot.binding().commit.changed_paths_digest
    );
    assert!(record.evidence_id.starts_with("commit:"));

    let search_request = EvidenceRequest {
        operation: EvidenceOperation::Search {
            search: SearchSelector {
                lane: SearchLane::Literal,
                query: "evidence_token".to_string(),
                paths: vec![FILE_PATH.to_string()],
                language: Some("rust".to_string()),
                kind: None,
                limit: DEFAULT_RESULT_LIMIT,
                hybrid_identity: None,
            },
        },
        ..range_request.clone()
    };
    let all_search = run_success(&project, &home, &connections, &search_request)?;
    assert_eq!(all_search.items.len(), 2);
    let item_sizes = all_search
        .items
        .iter()
        .map(|item| serde_json::to_vec(item).map(|bytes| bytes.len()))
        .collect::<Result<Vec<_>, _>>()?;
    let one_item_budget = *item_sizes.iter().max().expect("search evidence items");
    assert!(item_sizes.iter().sum::<usize>() > one_item_budget);
    let mut page_request = search_request.clone();
    page_request.max_bytes = one_item_budget;
    let first_page = run_success(&project, &home, &connections, &page_request)?;
    assert!(!first_page.complete);
    assert_eq!(
        first_page.completeness,
        gobby_code::evidence::Completeness::Paginated
    );
    assert_eq!(first_page.bounds.returned_items, 1);
    assert_eq!(first_page.bounds.total_items, 2);
    let continuation = first_page
        .continuation
        .clone()
        .expect("paginated response continuation");
    let mut continuation_request = page_request.clone();
    continuation_request.continuation = Some(continuation.clone());
    let second_page = run_success(&project, &home, &connections, &continuation_request)?;
    assert!(second_page.complete);
    assert_eq!(second_page.bounds.returned_items, 1);
    assert!(second_page.continuation.is_none());
    let mut paged_items = first_page.items.clone();
    paged_items.extend(second_page.items.clone());
    assert_eq!(paged_items, all_search.items);

    let mut bad_continuation = continuation_request;
    bad_continuation.continuation = Some(format!("{continuation}x"));
    assert_request_error(
        &project,
        &home,
        &connections,
        &bad_continuation,
        "continuation_mismatch",
    )?;

    let mut oversized = range_request.clone();
    oversized.max_bytes = 1;
    assert_request_error(
        &project,
        &home,
        &connections,
        &oversized,
        "narrowing_required",
    )?;

    let mut mismatched = range_request.clone();
    mismatched.binding.tree_oid = "0".repeat(40);
    assert_request_error(
        &project,
        &home,
        &connections,
        &mismatched,
        "snapshot_binding_mismatch",
    )?;

    let mut missing = range_request.clone();
    missing.binding.commit_oid = "0".repeat(40);
    assert_request_error(
        &project,
        &home,
        &connections,
        &missing,
        "missing_git_object",
    )?;
    let mut invalid_oid = range_request.clone();
    invalid_oid.binding.commit_oid = "HEAD".to_string();
    assert_request_error(
        &project,
        &home,
        &connections,
        &invalid_oid,
        "invalid_object_id",
    )?;

    let incompatible = EvidenceRequest {
        operation: EvidenceOperation::Search {
            search: SearchSelector {
                lane: SearchLane::Literal,
                query: "evidence_token".to_string(),
                paths: Vec::new(),
                language: None,
                kind: Some("function".to_string()),
                limit: DEFAULT_RESULT_LIMIT,
                hybrid_identity: None,
            },
        },
        ..range_request.clone()
    };
    assert_request_error(
        &project,
        &home,
        &connections,
        &incompatible,
        "invalid_selector",
    )?;
    let unsafe_path = EvidenceRequest {
        operation: EvidenceOperation::Read {
            read: ReadSelector::Range {
                path: "../secret.rs".to_string(),
                start_line: 1,
                end_line: 1,
            },
        },
        ..range_request.clone()
    };
    assert_request_error(&project, &home, &connections, &unsafe_path, "unsafe_path")?;

    assert_audited_hybrid_contract(
        &project,
        &home,
        &database_url,
        &range_request,
        first_symbol_id(&mut conn)?,
    )?;

    let graph = EvidenceRequest {
        operation: EvidenceOperation::Graph {
            graph: GraphSelector {
                query: GraphQuery::Imports,
                source: Some(EntitySelector::Path {
                    path: FILE_PATH.to_string(),
                }),
                target: None,
                direction: None,
                depth: DEFAULT_GRAPH_DEPTH,
                relations: Vec::new(),
                limit: DEFAULT_RESULT_LIMIT,
            },
        },
        ..range_request.clone()
    };
    assert_request_error(&project, &home, &connections, &graph, "graph_unavailable")?;

    let raw_request = serde_json::to_string(&range_request)?;
    let allow_stale = run_raw_evidence(&project, &home, &connections, &raw_request, true)?;
    assert_error(&allow_stale, "stale_admission_bypass_forbidden");
    let text_format =
        run_raw_evidence_with_format(&project, &home, &connections, &raw_request, "text")?;
    assert_error(&text_format, "unsupported_evidence_format");

    let mut malformed_selector = serde_json::to_value(&range_request)?;
    malformed_selector["read"]["kind"] = Value::String("unknown".to_string());
    let malformed_selector = run_raw_evidence(
        &project,
        &home,
        &connections,
        &serde_json::to_string(&malformed_selector)?,
        false,
    )?;
    assert_error(&malformed_selector, "invalid_evidence_request");

    std::fs::write(project.join(FILE_PATH), SOURCE.replace("needle", "changed"))?;
    git(&project, &["add", FILE_PATH])?;
    let stale_commit = commit(&project, "change without reindex")?;
    let stale_snapshot = Snapshot::prepare(&project, PROJECT_ID, &stale_commit)?;
    let stale_request = EvidenceRequest {
        binding: stale_snapshot.binding().clone(),
        operation: EvidenceOperation::Search {
            search: SearchSelector {
                lane: SearchLane::Literal,
                query: "evidence_token".to_string(),
                paths: Vec::new(),
                language: None,
                kind: None,
                limit: DEFAULT_RESULT_LIMIT,
                hybrid_identity: None,
            },
        },
        ..range_request.clone()
    };
    assert_request_error(
        &project,
        &home,
        &connections,
        &stale_request,
        "fact_snapshot_mismatch",
    )?;

    assert_eq!(
        fact_count(&mut conn)?,
        facts_before,
        "evidence must not write facts"
    );
    assert_eq!(
        indexed_hash(&mut conn)?,
        indexed_hash_before,
        "stale evidence must not autoindex"
    );
    assert_eq!(
        git(&project, &["status", "--porcelain"])?,
        status_before,
        "evidence must not mutate the source checkout"
    );

    let unavailable = gobby_core::grant::DirectConnections::postgres(
        "postgresql://gobby_test:gobby_test@127.0.0.1:1/unavailable_test",
    );
    let unavailable_output = run_raw_evidence(&project, &home, &unavailable, &raw_request, false)?;
    assert_error(&unavailable_output, "index_unavailable");

    drop(cleanup);
    Ok(())
}

#[cfg(gcode_postgres_tests)]
fn assert_pure_preflight_rejections(
    binding: &gobby_code::evidence::SnapshotBinding,
) -> anyhow::Result<()> {
    use gobby_code::evidence::{
        DEFAULT_GRAPH_DEPTH, DEFAULT_MAX_BYTES, DEFAULT_RESULT_LIMIT, EvidenceOperation,
        EvidenceRequest, GraphQuery, GraphSelector, ReadSelector, SearchLane, SearchSelector,
    };

    let root_dir = tempfile::tempdir()?;
    let root = root_dir.path().canonicalize()?;
    std::fs::create_dir_all(root.join(".gobby"))?;
    std::fs::write(
        root.join(".gobby/project.json"),
        serde_json::json!({"id": PROJECT_ID, "name": "pure-evidence-preflight"}).to_string(),
    )?;
    let home = isolated_gobby_home(&root)?;
    let unavailable = gobby_core::grant::DirectConnections::postgres(
        "postgresql://gobby_test:gobby_test@127.0.0.1:1/unavailable_test",
    );
    let base = EvidenceRequest {
        schema_version: 1,
        binding: binding.clone(),
        operation: EvidenceOperation::Read {
            read: ReadSelector::CommitMetadata,
        },
        max_bytes: DEFAULT_MAX_BYTES,
        continuation: None,
    };
    let unsupported_schema = EvidenceRequest {
        schema_version: 2,
        ..base.clone()
    };
    assert_request_error(
        &root,
        &home,
        &unavailable,
        &unsupported_schema,
        "unsupported_schema",
    )?;
    let missing_selector = EvidenceRequest {
        operation: EvidenceOperation::Graph {
            graph: GraphSelector {
                query: GraphQuery::Callers,
                source: None,
                target: None,
                direction: None,
                depth: DEFAULT_GRAPH_DEPTH,
                relations: Vec::new(),
                limit: DEFAULT_RESULT_LIMIT,
            },
        },
        ..base.clone()
    };
    assert_request_error(
        &root,
        &home,
        &unavailable,
        &missing_selector,
        "invalid_selector",
    )?;
    let unsafe_path = EvidenceRequest {
        operation: EvidenceOperation::Read {
            read: ReadSelector::Range {
                path: "../outside.rs".to_string(),
                start_line: 1,
                end_line: 1,
            },
        },
        ..base.clone()
    };
    assert_request_error(&root, &home, &unavailable, &unsafe_path, "unsafe_path")?;
    let missing_identity = EvidenceRequest {
        operation: EvidenceOperation::Search {
            search: SearchSelector {
                lane: SearchLane::Hybrid,
                query: "semantic evidence".to_string(),
                paths: Vec::new(),
                language: None,
                kind: None,
                limit: DEFAULT_RESULT_LIMIT,
                hybrid_identity: None,
            },
        },
        ..base
    };
    assert_request_error(
        &root,
        &home,
        &unavailable,
        &missing_identity,
        "semantic_identity_required",
    )
}

#[cfg(gcode_postgres_tests)]
fn assert_audited_hybrid_contract(
    project: &std::path::Path,
    home: &std::path::Path,
    database_url: &str,
    base: &gobby_code::evidence::EvidenceRequest,
    symbol_id: String,
) -> anyhow::Result<()> {
    use common::http::{spawn_http_responses, spawn_http_responses_after_probes};
    use gobby_code::evidence::{
        EvidenceOperation, EvidenceRequest, HybridIdentity, SearchLane, SearchSelector,
    };

    const ENDPOINT: &str = "https://embedding.example.invalid/v1";
    const MODEL: &str = "audited-model";
    const DIMENSION: usize = 3;
    let index_id = format!("code_symbols_{PROJECT_ID}");
    let identity = HybridIdentity {
        endpoint: ENDPOINT.to_string(),
        model: MODEL.to_string(),
        dimension: DIMENSION,
        index_id: index_id.clone(),
    };
    let request = EvidenceRequest {
        operation: EvidenceOperation::Search {
            search: SearchSelector {
                lane: SearchLane::Hybrid,
                query: "semantic-only-query".to_string(),
                paths: Vec::new(),
                language: None,
                kind: None,
                limit: 1,
                hybrid_identity: Some(identity.clone()),
            },
        },
        ..base.clone()
    };

    let deterministic = EvidenceRequest {
        operation: EvidenceOperation::Search {
            search: SearchSelector {
                lane: SearchLane::Literal,
                query: "evidence_token".to_string(),
                paths: Vec::new(),
                language: None,
                kind: None,
                limit: 1,
                hybrid_identity: None,
            },
        },
        ..base.clone()
    };
    let dead_services = gobby_core::grant::DirectConnections::postgres(database_url)
        .with_qdrant("http://127.0.0.1:1", None);
    let deterministic_output = run_raw_with_audited_services(
        project,
        home,
        &dead_services,
        "http://127.0.0.1:1",
        &serde_json::to_string(&deterministic)?,
    )?;
    anyhow::ensure!(
        deterministic_output.status.success(),
        "deterministic evidence touched unavailable semantic services: {}",
        String::from_utf8_lossy(&deterministic_output.stderr)
    );

    let collection_schema = serde_json::json!({
        "result": {"config": {"params": {"vectors": {
            "size": DIMENSION,
            "distance": "Cosine"
        }}}}
    });
    let (qdrant_url, qdrant) = spawn_http_responses(vec![
        (200, collection_schema.clone()),
        (
            200,
            serde_json::json!({
                "result": [{"id": symbol_id, "score": 0.9, "payload": {}}]
            }),
        ),
    ]);
    let (daemon_url, daemon) = spawn_http_responses_after_probes(vec![(
        200,
        serde_json::json!({
            "embeddings": [[0.1, 0.2, 0.3]],
            "model": MODEL,
            "dim": DIMENSION
        }),
    )]);
    let connections =
        gobby_core::grant::DirectConnections::postgres(database_url).with_qdrant(&qdrant_url, None);
    let response = run_with_audited_services(project, home, &connections, &daemon_url, &request)?;
    assert_eq!(response.contract.hybrid, Some(identity.clone()));
    assert_eq!(response.items.len(), 1);
    let qdrant_requests = qdrant.join().expect("Qdrant fixture thread")?;
    assert!(qdrant_requests[0].starts_with(&format!("GET /collections/{index_id} ")));
    assert!(
        qdrant_requests[1].starts_with(&format!("POST /collections/{index_id}/points/search "))
    );
    let daemon_requests = daemon.join().expect("daemon fixture thread")?;
    assert!(daemon_requests[0].starts_with("POST /api/embeddings "));
    let daemon_body: Value = serde_json::from_str(
        daemon_requests[0]
            .split_once("\r\n\r\n")
            .map(|(_, body)| body)
            .expect("daemon request body"),
    )?;
    assert_eq!(daemon_body["model"], MODEL);
    assert_eq!(daemon_body["is_query"], true);

    // A saturated backend page can contain only symbols invisible to this snapshot.
    // That is a bounded search, not proof that no visible semantic matches exist.
    let hidden_hits = (0..8)
        .map(|index| {
            serde_json::json!({
                "id": format!("00000000-0000-4000-8000-{index:012}"),
                "score": 0.9,
                "payload": {}
            })
        })
        .collect::<Vec<_>>();
    let (qdrant_url, qdrant) = spawn_http_responses(vec![
        (200, collection_schema.clone()),
        (200, serde_json::json!({"result": hidden_hits})),
    ]);
    let (daemon_url, daemon) = spawn_http_responses_after_probes(vec![(
        200,
        serde_json::json!({
            "embeddings": [[0.1, 0.2, 0.3]],
            "model": MODEL,
            "dim": DIMENSION
        }),
    )]);
    let connections =
        gobby_core::grant::DirectConnections::postgres(database_url).with_qdrant(&qdrant_url, None);
    let bounded = run_with_audited_services(project, home, &connections, &daemon_url, &request)?;
    qdrant.join().expect("bounded Qdrant fixture thread")?;
    daemon.join().expect("bounded embedding fixture thread")?;
    assert!(bounded.items.is_empty());
    assert!(
        !bounded.complete,
        "filtered saturated vector page is not complete"
    );
    assert_eq!(
        bounded.completeness,
        gobby_code::evidence::Completeness::TruncatedIndex
    );

    let (qdrant_url, qdrant) = spawn_http_responses(vec![(200, collection_schema.clone())]);
    let connections =
        gobby_core::grant::DirectConnections::postgres(database_url).with_qdrant(&qdrant_url, None);
    let mut changed = request.clone();
    let EvidenceOperation::Search { search } = &mut changed.operation else {
        unreachable!("hybrid request remains a search")
    };
    search
        .hybrid_identity
        .as_mut()
        .expect("hybrid identity")
        .model = "changed-model".to_string();
    let output = run_raw_with_audited_services(
        project,
        home,
        &connections,
        "http://127.0.0.1:1",
        &serde_json::to_string(&changed)?,
    )?;
    assert_error(&output, "semantic_identity_mismatch");
    qdrant.join().expect("Qdrant mismatch fixture thread")?;

    let (qdrant_url, qdrant) = spawn_http_responses(vec![(404, serde_json::json!({}))]);
    let connections =
        gobby_core::grant::DirectConnections::postgres(database_url).with_qdrant(&qdrant_url, None);
    let output = run_raw_with_audited_services(
        project,
        home,
        &connections,
        "http://127.0.0.1:1",
        &serde_json::to_string(&request)?,
    )?;
    assert_error(&output, "semantic_failure");
    qdrant.join().expect("missing Qdrant fixture thread")?;

    let (qdrant_url, qdrant) = spawn_http_responses(vec![(200, collection_schema)]);
    let (daemon_url, daemon) =
        spawn_http_responses_after_probes(vec![(400, serde_json::json!({"error": "denied"}))]);
    let connections =
        gobby_core::grant::DirectConnections::postgres(database_url).with_qdrant(&qdrant_url, None);
    let output = run_raw_with_audited_services(
        project,
        home,
        &connections,
        &daemon_url,
        &serde_json::to_string(&request)?,
    )?;
    assert_error(&output, "semantic_failure");
    qdrant.join().expect("Qdrant provider fixture thread")?;
    daemon.join().expect("daemon failure fixture thread")?;

    Ok(())
}

#[cfg(gcode_postgres_tests)]
fn run_with_audited_services(
    project: &std::path::Path,
    home: &std::path::Path,
    connections: &gobby_core::grant::DirectConnections,
    daemon_url: &str,
    request: &gobby_code::evidence::EvidenceRequest,
) -> anyhow::Result<gobby_code::evidence::EvidenceResponse> {
    let output = run_raw_with_audited_services(
        project,
        home,
        connections,
        daemon_url,
        &serde_json::to_string(request)?,
    )?;
    anyhow::ensure!(
        output.status.success(),
        "audited hybrid evidence request failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(serde_json::from_slice(&output.stdout)?)
}

#[cfg(gcode_postgres_tests)]
fn run_raw_with_audited_services(
    project: &std::path::Path,
    home: &std::path::Path,
    connections: &gobby_core::grant::DirectConnections,
    daemon_url: &str,
    request_json: &str,
) -> anyhow::Result<std::process::Output> {
    let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
    command
        .current_dir(project)
        .args(["--quiet", "--project"])
        .arg(project)
        .args(["evidence", "--request-json", request_json]);
    attach_audited_hybrid_grant(&mut command, home, PROJECT_ID, connections, daemon_url)?;
    Ok(command.output()?)
}

#[cfg(gcode_postgres_tests)]
fn attach_audited_hybrid_grant(
    command: &mut Command,
    home: &std::path::Path,
    project_id: &str,
    connections: &gobby_core::grant::DirectConnections,
    daemon_url: &str,
) -> anyhow::Result<()> {
    use std::collections::BTreeMap;

    let machine = std::fs::read_to_string(home.join("machine_id"))?;
    let mut grant =
        gobby_core::grant::managed_direct_grant(project_id, machine.trim(), connections);
    grant.capabilities.embed = gobby_core::grant::AiCapability::Daemon {};
    grant = grant.with_checksum();
    let settings = gobby_core::grant::CachedSettings {
        config_revision: grant.config_revision,
        settings: BTreeMap::from([
            ("ai.embeddings.routing".to_string(), "daemon".to_string()),
            (
                "ai.embeddings.transport".to_string(),
                "openai_compatible_http".to_string(),
            ),
            (
                "ai.embeddings.provider".to_string(),
                "audited-provider".to_string(),
            ),
            (
                "ai.embeddings.api_base".to_string(),
                "https://embedding.example.invalid/v1".to_string(),
            ),
            (
                "ai.embeddings.model".to_string(),
                "audited-model".to_string(),
            ),
            ("ai.embeddings.dim".to_string(), "3".to_string()),
        ]),
    };
    let grant_dir = home.join("grants/audited-hybrid");
    std::fs::create_dir_all(&grant_dir)?;
    let path = grant_dir.join("grant.json");
    gobby_core::grant::write_coherent_pair(&path, &grant, &settings)?;
    std::fs::write(home.join("local_cli_token"), "audited-test-token\n")?;
    command
        .env("GOBBY_HOME", home)
        .env("GOBBY_DAEMON_URL", daemon_url)
        .env("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", path)
        .env_remove("GOBBY_AGENT_API_TOKEN")
        .env_remove("GOBBY_AGENT_RUN_ID")
        .env_remove("GOBBY_MANAGED_EXECUTION_ID");
    Ok(())
}

#[cfg(gcode_postgres_tests)]
fn run_success(
    project: &std::path::Path,
    home: &std::path::Path,
    connections: &gobby_core::grant::DirectConnections,
    request: &gobby_code::evidence::EvidenceRequest,
) -> anyhow::Result<gobby_code::evidence::EvidenceResponse> {
    let output = run_raw_evidence(
        project,
        home,
        connections,
        &serde_json::to_string(request)?,
        false,
    )?;
    anyhow::ensure!(
        output.status.success(),
        "evidence request failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(serde_json::from_slice(&output.stdout)?)
}

#[cfg(gcode_postgres_tests)]
fn assert_request_error(
    project: &std::path::Path,
    home: &std::path::Path,
    connections: &gobby_core::grant::DirectConnections,
    request: &gobby_code::evidence::EvidenceRequest,
    code: &str,
) -> anyhow::Result<()> {
    let output = run_raw_evidence(
        project,
        home,
        connections,
        &serde_json::to_string(request)?,
        false,
    )?;
    assert_error(&output, code);
    Ok(())
}

#[cfg(gcode_postgres_tests)]
fn run_raw_evidence(
    project: &std::path::Path,
    home: &std::path::Path,
    connections: &gobby_core::grant::DirectConnections,
    request_json: &str,
    allow_stale: bool,
) -> anyhow::Result<std::process::Output> {
    let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
    command
        .current_dir(project)
        .arg("--quiet")
        .arg("--project")
        .arg(project);
    if allow_stale {
        command.arg("--allow-stale");
    }
    command.args(["evidence", "--request-json", request_json]);
    attach_managed_grant(&mut command, home, PROJECT_ID, connections)?;
    Ok(command.output()?)
}

#[cfg(gcode_postgres_tests)]
fn run_raw_evidence_with_format(
    project: &std::path::Path,
    home: &std::path::Path,
    connections: &gobby_core::grant::DirectConnections,
    request_json: &str,
    format: &str,
) -> anyhow::Result<std::process::Output> {
    let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
    command
        .current_dir(project)
        .args(["--quiet", "--format", format, "--project"])
        .arg(project)
        .args(["evidence", "--request-json", request_json]);
    attach_managed_grant(&mut command, home, PROJECT_ID, connections)?;
    Ok(command.output()?)
}

#[cfg(gcode_postgres_tests)]
fn isolated_gobby_home(project: &std::path::Path) -> anyhow::Result<std::path::PathBuf> {
    let home = project.join(".test-gobby-home");
    std::fs::create_dir_all(&home)?;
    std::fs::write(
        home.join("machine_id"),
        gobby_core::machine::read_local_machine_id()?,
    )?;
    Ok(home)
}

#[cfg(gcode_postgres_tests)]
fn attach_managed_grant(
    command: &mut Command,
    home: &std::path::Path,
    project_id: &str,
    connections: &gobby_core::grant::DirectConnections,
) -> anyhow::Result<()> {
    let machine = std::fs::read_to_string(home.join("machine_id"))?;
    let grant = gobby_core::grant::managed_direct_grant(project_id, machine.trim(), connections);
    let path = gobby_core::grant::write_managed_bootstrap(&home.join("grants"), &grant)?;
    command
        .env("GOBBY_HOME", home)
        .env("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", path)
        .env_remove("GOBBY_AGENT_RUN_ID")
        .env_remove("GOBBY_MANAGED_EXECUTION_ID");
    Ok(())
}

#[cfg(gcode_postgres_tests)]
fn git(repo: &std::path::Path, args: &[&str]) -> anyhow::Result<String> {
    let output = Command::new("git")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()?;
    anyhow::ensure!(
        output.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(String::from_utf8(output.stdout)?.trim().to_string())
}

#[cfg(gcode_postgres_tests)]
fn commit(repo: &std::path::Path, message: &str) -> anyhow::Result<String> {
    git(
        repo,
        &[
            "-c",
            "user.name=Evidence CLI Test",
            "-c",
            "user.email=evidence-cli@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        ],
    )?;
    git(repo, &["rev-parse", "HEAD"])
}

#[cfg(gcode_postgres_tests)]
fn uuid_param() -> uuid::Uuid {
    uuid::Uuid::parse_str(PROJECT_ID).expect("fixture project ID is a UUID")
}

#[cfg(gcode_postgres_tests)]
fn fact_count(conn: &mut postgres::Client) -> anyhow::Result<i64> {
    Ok(conn
        .query_one(
            "SELECT
                (SELECT count(*) FROM code_indexed_files WHERE project_id = $1)
              + (SELECT count(*) FROM code_symbols WHERE project_id = $1)
              + (SELECT count(*) FROM code_content_chunks WHERE project_id = $1)",
            &[&uuid_param()],
        )?
        .get(0))
}

#[cfg(gcode_postgres_tests)]
fn indexed_hash(conn: &mut postgres::Client) -> anyhow::Result<String> {
    Ok(conn
        .query_one(
            "SELECT content_hash FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2",
            &[&uuid_param(), &FILE_PATH],
        )?
        .get(0))
}

#[cfg(gcode_postgres_tests)]
fn first_symbol_id(conn: &mut postgres::Client) -> anyhow::Result<String> {
    Ok(conn
        .query_one(
            "SELECT id::text FROM code_symbols WHERE project_id = $1 ORDER BY id LIMIT 1",
            &[&uuid_param()],
        )?
        .get(0))
}

#[cfg(gcode_postgres_tests)]
struct ProjectCleanup {
    database_url: String,
}

#[cfg(gcode_postgres_tests)]
impl Drop for ProjectCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = postgres::Client::connect(&self.database_url, postgres::NoTls) {
            let _ = cleanup_project(&mut conn, PROJECT_ID);
        }
    }
}

#[cfg(gcode_postgres_tests)]
fn cleanup_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
    let project_id = uuid::Uuid::parse_str(project_id)?;
    conn.execute(
        "DELETE FROM code_indexed_file_states WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_project_states WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_calls
         WHERE project_id = $1
            OR caller_symbol_id IN (SELECT id FROM code_symbols WHERE project_id = $1)
            OR callee_symbol_id IN (SELECT id FROM code_symbols WHERE project_id = $1)",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM project_checkouts WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute("DELETE FROM projects WHERE id = $1", &[&project_id])?;
    Ok(())
}
