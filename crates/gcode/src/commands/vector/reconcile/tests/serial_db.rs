use super::super::{
    Inventory, MissingClass, MissingGroup, OrphanClass, ReconcileReceipt, apply,
    apply_with_persist, inventory, inventory_digest, reset_synced_missing_versions,
};
use super::canonical_tempdir;
use crate::config::{
    CODE_SYMBOL_COLLECTION_PREFIX, CodeVectorSettings, Context, ProjectIndexScope, QdrantConfig,
};
use crate::db;
use crate::index_lock::{IndexLockPolicy, lease_project_lock};
use crate::models::{CODE_INDEX_UUID_NAMESPACE, Symbol};
use crate::vector::code_symbols;
use crate::vector::code_symbols::tests::spawn_http_responses;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use uuid::Uuid;

/// A project with seeded PostgreSQL versions, symbols and machine references.
struct Fixture {
    conn: postgres::Client,
    database_url: String,
    project_id: String,
    machine_id: String,
}

impl Fixture {
    fn new() -> anyhow::Result<Self> {
        let database_url =
            crate::test_env::postgres_test_database_url("vector reconcile dry-run tests");
        let mut conn = db::connect_readwrite(&database_url)?;
        let project_id = Uuid::new_v4().to_string();
        let machine_id = gobby_core::machine::read_local_machine_id()?;
        let pid = db::id_param(&project_id)?;
        conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, $2)",
            &[&pid, &format!("reconcile-{pid}")],
        )?;
        conn.execute(
            "INSERT INTO code_indexed_projects (id) VALUES ($1)",
            &[&pid],
        )?;
        // File states reference this machine's project state, which needs a checkout.
        let root_path = format!("/nonexistent/reconcile/{project_id}");
        let machine_uuid = db::id_param(&machine_id)?;
        conn.execute(
            "INSERT INTO project_checkouts (machine_id, project_id, root_path) VALUES ($1, $2, $3)",
            &[&machine_uuid, &pid, &root_path],
        )?;
        conn.execute(
            "INSERT INTO code_indexed_project_states (machine_id, project_id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms) VALUES ($1, $2, $3, 0, 0, NOW(), 0)",
            &[&machine_uuid, &pid, &root_path],
        )?;
        Ok(Self {
            conn,
            database_url,
            project_id,
            machine_id,
        })
    }

    /// Insert a version row and return its `code_indexed_files.id`.
    fn version(
        &mut self,
        file_path: &str,
        content_hash: &str,
        synced: bool,
    ) -> anyhow::Result<String> {
        let id = Uuid::new_v4().to_string();
        self.conn.execute(
            "INSERT INTO code_indexed_files (id,project_id,file_path,language,content_hash,symbol_count,byte_size,graph_synced,vectors_synced,indexed_at,last_referenced_at) VALUES ($1,$2,$3,'rust',$4,0,20,true,$5,NOW(),NOW())",
            &[&db::id_param(&id)?, &db::id_param(&self.project_id)?, &file_path, &content_hash, &synced],
        )?;
        Ok(id)
    }

    /// Mark a version as referenced by this machine's file state.
    fn reference(&mut self, file_path: &str, content_hash: &str) -> anyhow::Result<()> {
        self.conn.execute(
            "INSERT INTO code_indexed_file_states (machine_id, project_id, file_path, content_hash) VALUES ($1, $2, $3, $4)",
            &[&db::id_param(&self.machine_id)?, &db::id_param(&self.project_id)?, &file_path, &content_hash],
        )?;
        Ok(())
    }

    /// Insert a symbol under a version and return its deterministic `Symbol::make_id`.
    fn symbol(
        &mut self,
        file_path: &str,
        content_hash: &str,
        name: &str,
        kind: &str,
        byte_start: usize,
    ) -> anyhow::Result<String> {
        let id = Symbol::make_id(
            &self.project_id,
            file_path,
            content_hash,
            name,
            kind,
            byte_start,
        );
        let byte_start = i32::try_from(byte_start)?;
        self.conn.execute(
            "INSERT INTO code_symbols (id,project_id,file_path,name,qualified_name,kind,language,byte_start,byte_end,line_start,line_end,file_content_hash,content_hash) VALUES ($1,$2,$3,$4,$4,$5,'rust',$6,$6,1,1,$7,'sym')",
            &[
                &db::id_param(&id)?,
                &db::id_param(&self.project_id)?,
                &file_path,
                &name,
                &kind,
                &byte_start,
                &content_hash,
            ],
        )?;
        Ok(id)
    }

    fn context(&self, qdrant_url: String) -> Context {
        Context {
            database_url: self.database_url.clone(),
            project_root: PathBuf::from("/nonexistent"),
            project_id: self.project_id.clone(),
            quiet: true,
            falkordb: None,
            qdrant: Some(QdrantConfig {
                url: Some(qdrant_url),
                api_key: None,
            }),
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            machine_id: self.machine_id.clone(),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: ProjectIndexScope::Single,
        }
    }

    fn vectors_synced(&mut self, version_id: &str) -> anyhow::Result<bool> {
        let row = self.conn.query_one(
            "SELECT vectors_synced FROM code_indexed_files WHERE id = $1",
            &[&db::id_param(version_id)?],
        )?;
        Ok(row.get(0))
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let Ok(pid) = db::id_param(&self.project_id) else {
            return;
        };
        for table in [
            "code_symbols",
            "code_indexed_file_states",
            "code_indexed_files",
            "code_indexed_project_states",
            "project_checkouts",
            "code_indexed_projects",
            "projects",
        ] {
            let column = if table == "projects" {
                "id"
            } else {
                "project_id"
            };
            let sql = format!("DELETE FROM {table} WHERE {column} = $1");
            let _ = self.conn.execute(sql.as_str(), &[&pid]);
        }
    }
}

/// One Qdrant scroll page holding the given `(point id, payload)` pairs.
fn scroll_page(points: Vec<(String, Value)>) -> (u16, Value) {
    let points: Vec<Value> = points
        .into_iter()
        .map(|(id, payload)| json!({"id": id, "payload": payload}))
        .collect();
    (
        200,
        json!({"result": {"points": points, "next_page_offset": null}, "status": "ok", "time": 0.0}),
    )
}

fn payload(file_path: &str, name: &str, kind: &str, byte_start: usize) -> Value {
    json!({"file_path": file_path, "name": name, "kind": kind, "byte_start": byte_start})
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn dry_run_classifies_only_ids_absent_from_postgres() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let live_path = "src/live.rs";
    let live_hash = "a".repeat(64);
    fixture.version(live_path, &live_hash, true)?;
    fixture.reference(live_path, &live_hash)?;
    let live = fixture.symbol(live_path, &live_hash, "live_fn", "function", 0)?;
    let live_without_payload = fixture.symbol(live_path, &live_hash, "second", "function", 40)?;

    // A retained version whose symbols no longer exist in PostgreSQL.
    let old_path = "src/old.rs";
    let old_hash = "b".repeat(64);
    fixture.version(old_path, &old_hash, true)?;

    let project = fixture.project_id.clone();
    let legacy_payload_id = Uuid::new_v4().to_string();
    let legacy_scheme_id = Uuid::new_v5(
        &CODE_INDEX_UUID_NAMESPACE,
        format!("{project}:src/gone.rs:fn_a:function:10").as_bytes(),
    )
    .to_string();
    let reparsed_id = Symbol::make_id(&project, old_path, &old_hash, "fn_b", "function", 20);
    let unattributed_id = Uuid::new_v4().to_string();

    let (url, handle) = spawn_http_responses(vec![scroll_page(vec![
        (live.clone(), payload(live_path, "live_fn", "function", 0)),
        // A live ID with no file_path in its payload is still not an orphan.
        (live_without_payload, json!({})),
        (legacy_payload_id.clone(), json!({})),
        (
            legacy_scheme_id.clone(),
            payload("src/gone.rs", "fn_a", "function", 10),
        ),
        (
            reparsed_id.clone(),
            payload(old_path, "fn_b", "function", 20),
        ),
        (
            unattributed_id.clone(),
            payload("src/none.rs", "x", "function", 1),
        ),
    ])]);
    let report = inventory(&fixture.context(url))?;
    handle.join().expect("qdrant test server");

    assert_eq!(report.points_scanned, 6);
    assert_eq!(report.pg_symbols, 2);
    assert_eq!(
        report.orphans[&OrphanClass::LegacyPayload],
        vec![legacy_payload_id.clone()]
    );
    assert_eq!(
        report.orphans[&OrphanClass::LegacyIdScheme],
        vec![legacy_scheme_id.clone()]
    );
    assert_eq!(
        report.orphans[&OrphanClass::ReparsedVersion],
        vec![reparsed_id.clone()]
    );
    assert_eq!(
        report.orphans[&OrphanClass::Unattributed],
        vec![unattributed_id.clone()]
    );
    assert_eq!(
        report.inventory_digest,
        inventory_digest(
            &[
                legacy_payload_id,
                legacy_scheme_id,
                reparsed_id,
                unattributed_id
            ],
            &[],
        ),
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn dry_run_splits_missing_vectors_by_flag_and_reference() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;

    // Synced and referenced: one of two symbol points is missing.
    let a_hash = "1".repeat(64);
    let a_version = fixture.version("src/a.rs", &a_hash, true)?;
    fixture.reference("src/a.rs", &a_hash)?;
    let a_present = fixture.symbol("src/a.rs", &a_hash, "a1", "function", 0)?;
    fixture.symbol("src/a.rs", &a_hash, "a2", "function", 10)?;

    // Synced and retained by no machine: both symbol points are missing.
    let b_hash = "2".repeat(64);
    let b_version = fixture.version("src/b.rs", &b_hash, true)?;
    fixture.symbol("src/b.rs", &b_hash, "b1", "function", 0)?;
    fixture.symbol("src/b.rs", &b_hash, "b2", "function", 10)?;

    // Pending (flag false) and referenced.
    let c_hash = "3".repeat(64);
    let c_version = fixture.version("src/c.rs", &c_hash, false)?;
    fixture.reference("src/c.rs", &c_hash)?;
    fixture.symbol("src/c.rs", &c_hash, "c1", "function", 0)?;

    // Pending (flag false) and retained by no machine; one of two points is present.
    let d_hash = "4".repeat(64);
    let d_version = fixture.version("src/d.rs", &d_hash, false)?;
    let d_present = fixture.symbol("src/d.rs", &d_hash, "d1", "function", 0)?;
    fixture.symbol("src/d.rs", &d_hash, "d2", "function", 10)?;

    // Complete version: every symbol point present.
    let e_hash = "5".repeat(64);
    fixture.version("src/e.rs", &e_hash, true)?;
    fixture.reference("src/e.rs", &e_hash)?;
    let e_symbol = fixture.symbol("src/e.rs", &e_hash, "e1", "function", 0)?;

    // Synced version with no symbols at all is not missing anything.
    let f_hash = "6".repeat(64);
    fixture.version("src/f.rs", &f_hash, true)?;

    let (url, handle) = spawn_http_responses(vec![scroll_page(vec![
        (a_present, payload("src/a.rs", "a1", "function", 0)),
        (d_present, payload("src/d.rs", "d1", "function", 0)),
        (e_symbol, payload("src/e.rs", "e1", "function", 0)),
    ])]);
    let report = inventory(&fixture.context(url))?;
    handle.join().expect("qdrant test server");

    assert_eq!(
        report.missing[&MissingClass::SyncedMissingCurrent],
        MissingGroup {
            version_ids: vec![a_version.clone()],
            symbols: 1,
        },
    );
    assert_eq!(
        report.missing[&MissingClass::SyncedMissingRetained],
        MissingGroup {
            version_ids: vec![b_version.clone()],
            symbols: 2,
        },
    );
    assert_eq!(
        report.missing[&MissingClass::PendingCurrent],
        MissingGroup {
            version_ids: vec![c_version.clone()],
            symbols: 1,
        },
    );
    assert_eq!(
        report.missing[&MissingClass::PendingRetained],
        MissingGroup {
            version_ids: vec![d_version.clone()],
            symbols: 1,
        },
    );
    assert_eq!(
        report.inventory_digest,
        inventory_digest(&[], &[a_version, b_version]),
        "pending versions must not change the digest",
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn dry_run_mutates_nothing() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let hash = "7".repeat(64);
    let version = fixture.version("src/m.rs", &hash, true)?;
    fixture.reference("src/m.rs", &hash)?;
    fixture.symbol("src/m.rs", &hash, "m1", "function", 0)?;
    let pending = fixture.version("src/n.rs", &"8".repeat(64), false)?;

    let (url, handle) = spawn_http_responses(vec![scroll_page(vec![(
        Uuid::new_v4().to_string(),
        payload("src/gone.rs", "g", "function", 0),
    )])]);
    let report = inventory(&fixture.context(url))?;
    let requests = handle.join().expect("qdrant test server");

    assert_eq!(report.orphans[&OrphanClass::Unattributed].len(), 1);
    assert_eq!(requests.len(), 1, "one scroll page and nothing else");
    assert!(
        requests
            .iter()
            .all(|request| request.starts_with("POST ") && request.contains("/points/scroll")),
        "dry run may send only scroll requests: {requests:?}",
    );
    assert!(
        fixture.vectors_synced(&version)?,
        "synced flag must stay true"
    );
    assert!(
        !fixture.vectors_synced(&pending)?,
        "pending flag must stay false"
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn dry_run_treats_missing_collection_as_empty() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let hash = "9".repeat(64);
    let version = fixture.version("src/z.rs", &hash, true)?;
    fixture.reference("src/z.rs", &hash)?;
    fixture.symbol("src/z.rs", &hash, "z1", "function", 0)?;
    fixture.symbol("src/z.rs", &hash, "z2", "function", 10)?;

    let (url, handle) = spawn_http_responses(vec![(
        404,
        json!({"status": {"error": "Not found: Collection doesn't exist"}, "time": 0.0}),
    )]);
    let report: Inventory = inventory(&fixture.context(url))?;
    handle.join().expect("qdrant test server");

    assert_eq!(report.points_scanned, 0);
    assert_eq!(report.pg_symbols, 2);
    assert!(
        report.orphans.values().all(Vec::is_empty),
        "an empty collection has no orphans",
    );
    assert_eq!(
        report.missing[&MissingClass::SyncedMissingCurrent],
        MissingGroup {
            version_ids: vec![version],
            symbols: 2,
        },
    );
    Ok(())
}

fn orphan_points(count: usize) -> Vec<(String, Value)> {
    (0..count)
        .map(|_| (Uuid::new_v4().to_string(), json!({})))
        .collect()
}

/// One synced version whose only symbol is absent from Qdrant, plus one orphan point.
/// Apply resets the version and deletes the orphan.
fn seed_reset_and_orphan(fixture: &mut Fixture) -> anyhow::Result<(Vec<(String, Value)>, String)> {
    let hash = "e".repeat(64);
    let version = fixture.version("src/reset.rs", &hash, true)?;
    fixture.reference("src/reset.rs", &hash)?;
    fixture.symbol("src/reset.rs", &hash, "reset1", "function", 0)?;
    Ok((orphan_points(1), version))
}

fn dry_run(fixture: &Fixture, points: Vec<(String, Value)>) -> anyhow::Result<Inventory> {
    let (url, handle) = spawn_http_responses(vec![scroll_page(points)]);
    let report = inventory(&fixture.context(url))?;
    handle.join().expect("qdrant test server");
    Ok(report)
}

fn delete_ok() -> (u16, Value) {
    (
        200,
        json!({"result": {"operation_id": 1, "status": "completed"}, "status": "ok", "time": 0.0}),
    )
}

fn delete_failed() -> (u16, Value) {
    (
        500,
        json!({"status": {"error": "injected delete failure"}, "time": 0.0}),
    )
}

fn delete_requests(requests: &[String]) -> Vec<&String> {
    requests
        .iter()
        .filter(|request| request.contains("/points/delete"))
        .collect()
}

/// Point IDs named by one `/points/delete` request.
fn deleted_ids(request: &str) -> Vec<String> {
    let body = request
        .split("\r\n\r\n")
        .nth(1)
        .expect("delete request has a body");
    let body: Value = serde_json::from_str(body).expect("delete body is JSON");
    body["points"]
        .as_array()
        .expect("delete body names points")
        .iter()
        .map(|id| id.as_str().expect("point ID is a string").to_string())
        .collect()
}

fn receipt_on_disk(path: &Path) -> ReconcileReceipt {
    let text = std::fs::read_to_string(path).expect("receipt is on disk");
    serde_json::from_str(&text).expect("receipt parses")
}

#[derive(Clone, Copy)]
enum WriteFailure {
    BeforeRecord,
    AfterRecord,
}

/// Receipt writer double. Write numbers are 1-based and count every receipt write.
#[derive(Default)]
struct Recorder {
    writes: usize,
    failure: Option<(usize, WriteFailure)>,
    recorded: Vec<ReconcileReceipt>,
}

impl Recorder {
    fn failing(write: usize, failure: WriteFailure) -> Self {
        Self {
            failure: Some((write, failure)),
            ..Self::default()
        }
    }

    fn persist(&mut self, receipt: &ReconcileReceipt) -> anyhow::Result<()> {
        self.writes += 1;
        let failure = self
            .failure
            .and_then(|(write, failure)| (write == self.writes).then_some(failure));
        match failure {
            Some(WriteFailure::BeforeRecord) => {
                anyhow::bail!("injected receipt write failure before recording")
            }
            Some(WriteFailure::AfterRecord) => {
                self.recorded.push(receipt.clone());
                anyhow::bail!("injected receipt write failure after publishing")
            }
            None => {
                self.recorded.push(receipt.clone());
                Ok(())
            }
        }
    }
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_deletes_only_disowned_ids_and_writes_receipt() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    // Synced and referenced, one of two symbol points missing: apply resets it.
    let a_hash = "a".repeat(64);
    let a_version = fixture.version("src/a.rs", &a_hash, true)?;
    fixture.reference("src/a.rs", &a_hash)?;
    let a_present = fixture.symbol("src/a.rs", &a_hash, "a1", "function", 0)?;
    fixture.symbol("src/a.rs", &a_hash, "a2", "function", 10)?;

    // Synced with every symbol point present: apply leaves it alone.
    let e_hash = "e".repeat(64);
    let e_version = fixture.version("src/e.rs", &e_hash, true)?;
    fixture.reference("src/e.rs", &e_hash)?;
    let e_symbol = fixture.symbol("src/e.rs", &e_hash, "e1", "function", 0)?;

    // Pending with a missing symbol point: already unsynced, not reset.
    let c_hash = "c".repeat(64);
    let c_version = fixture.version("src/c.rs", &c_hash, false)?;
    fixture.reference("src/c.rs", &c_hash)?;
    fixture.symbol("src/c.rs", &c_hash, "c1", "function", 0)?;

    let live = vec![
        (a_present, payload("src/a.rs", "a1", "function", 0)),
        (e_symbol, payload("src/e.rs", "e1", "function", 0)),
    ];
    let legacy_id = Uuid::new_v4().to_string();
    let unattributed_id = Uuid::new_v4().to_string();
    let mut points = live.clone();
    points.push((legacy_id.clone(), json!({})));
    points.push((
        unattributed_id.clone(),
        payload("src/none.rs", "x", "function", 1),
    ));

    let digest = dry_run(&fixture, points.clone())?.inventory_digest;
    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let (url, handle) = spawn_http_responses(vec![scroll_page(points), delete_ok()]);
    let receipt = apply(&fixture.context(url), &digest, &receipt_path)?;
    let requests = handle.join().expect("qdrant test server");

    let deletes = delete_requests(&requests);
    assert_eq!(deletes.len(), 1, "both orphans fit one batch: {requests:?}");
    let mut sent = deleted_ids(deletes[0]);
    sent.sort();
    let mut orphans = vec![legacy_id, unattributed_id];
    orphans.sort();
    assert_eq!(sent, orphans, "only disowned IDs are deleted");

    assert!(receipt.complete, "the apply run completes");
    assert_eq!(
        receipt,
        receipt_on_disk(&receipt_path),
        "the receipt on disk is the returned one"
    );
    assert_eq!(receipt.digest, digest);
    assert_eq!(receipt.versions_reset, vec![a_version.clone()]);
    let mut recorded: Vec<String> = receipt.deleted.iter().flatten().cloned().collect();
    recorded.sort();
    assert_eq!(
        recorded, orphans,
        "the receipt records every deleted orphan"
    );
    assert!(
        !fixture.vectors_synced(&a_version)?,
        "the synced-missing version is reset"
    );
    assert!(
        fixture.vectors_synced(&e_version)?,
        "a complete version keeps its flag"
    );
    assert!(
        !fixture.vectors_synced(&c_version)?,
        "a pending version stays pending"
    );

    let after = dry_run(&fixture, live)?;
    assert!(
        after.orphans.values().all(Vec::is_empty),
        "an immediate dry run finds no orphans"
    );
    assert!(
        after.missing[&MissingClass::SyncedMissingCurrent]
            .version_ids
            .is_empty()
            && after.missing[&MissingClass::SyncedMissingRetained]
                .version_ids
                .is_empty(),
        "an immediate dry run finds no synced-missing versions",
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_is_busy_without_mutation_when_lease_is_held() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let hash = "d".repeat(64);
    let version = fixture.version("src/busy.rs", &hash, true)?;
    fixture.reference("src/busy.rs", &hash)?;
    fixture.symbol("src/busy.rs", &hash, "busy1", "function", 0)?;
    fixture.symbol("src/busy.rs", &hash, "busy2", "function", 10)?;

    // Another session holds the project's maintenance lease.
    let mut holder = db::connect_readwrite(&fixture.database_url)?;
    let _held = lease_project_lock(
        &mut holder,
        &fixture.project_id,
        IndexLockPolicy::maintenance_try(),
    )?
    .expect("the holder takes the lease first");

    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let (url, handle) = spawn_http_responses(vec![]);
    let error = apply(
        &fixture.context(url),
        &format!("sha256:{}", "0".repeat(64)),
        &receipt_path,
    )
    .expect_err("a held lease refuses the apply");
    let requests = handle.join().expect("qdrant test server");

    assert!(
        error.to_string().contains("project index is busy"),
        "unexpected refusal: {error:#}",
    );
    assert!(
        requests.is_empty(),
        "no Qdrant request after a busy lease: {requests:?}"
    );
    assert!(!receipt_path.exists(), "no receipt is written");
    assert!(fixture.vectors_synced(&version)?, "the flag stays synced");
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_refuses_when_inventory_changes_under_lease() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let steady_hash = "5".repeat(64);
    let steady = fixture.version("src/steady.rs", &steady_hash, true)?;
    fixture.reference("src/steady.rs", &steady_hash)?;
    fixture.symbol("src/steady.rs", &steady_hash, "s1", "function", 0)?;

    // A pending version whose symbol is not yet in PostgreSQL: its point is a
    // reparsed-version orphan until the symbol row appears.
    let gained_hash = "6".repeat(64);
    fixture.version("src/gained.rs", &gained_hash, false)?;
    let gained_id = Symbol::make_id(
        &fixture.project_id,
        "src/gained.rs",
        &gained_hash,
        "gained",
        "function",
        20,
    );
    let points = vec![(
        gained_id,
        payload("src/gained.rs", "gained", "function", 20),
    )];
    let digest = dry_run(&fixture, points.clone())?.inventory_digest;

    // The symbol gains the orphan's ID between the dry run and apply.
    fixture.symbol("src/gained.rs", &gained_hash, "gained", "function", 20)?;

    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let (url, handle) = spawn_http_responses(vec![scroll_page(points)]);
    let error = apply(&fixture.context(url), &digest, &receipt_path)
        .expect_err("a changed inventory refuses the apply");
    let requests = handle.join().expect("qdrant test server");

    let message = format!("{error:#}");
    assert!(
        message.contains(&digest),
        "refusal names the expected digest: {message}"
    );
    assert_eq!(
        message.matches("sha256:").count(),
        2,
        "refusal names both digests: {message}"
    );
    assert_eq!(
        requests.len(),
        1,
        "only the rebuilt inventory's scroll is sent: {requests:?}"
    );
    assert!(!receipt_path.exists(), "no receipt is written");
    assert!(
        fixture.vectors_synced(&steady)?,
        "no flag changes under a refused apply"
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_records_partial_receipt_on_qdrant_failure() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let (mut points, version) = seed_reset_and_orphan(&mut fixture)?;
    // 257 orphans: batches of 256 and 1.
    points.extend(orphan_points(256));
    let digest = dry_run(&fixture, points.clone())?.inventory_digest;

    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let (url, handle) =
        spawn_http_responses(vec![scroll_page(points), delete_ok(), delete_failed()]);
    let error = apply(&fixture.context(url), &digest, &receipt_path)
        .expect_err("a failed Qdrant batch fails the run");
    let requests = handle.join().expect("qdrant test server");

    let deletes = delete_requests(&requests);
    assert_eq!(deletes.len(), 2, "the run stops at the failed second batch");
    let receipt = receipt_on_disk(&receipt_path);
    assert!(!receipt.complete, "a partial run is not complete");
    assert_eq!(
        receipt.deleted,
        vec![deleted_ids(deletes[0])],
        "the receipt lists the batch that was deleted",
    );
    assert_eq!(receipt.versions_reset, vec![version]);
    let message = format!("{error:#}");
    assert!(
        message.contains(&receipt_path.display().to_string()),
        "the error names the receipt: {message}"
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn index_repair_resets_synced_missing_versions_without_deleting() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let (points, reset_version) = seed_reset_and_orphan(&mut fixture)?;

    let (url, handle) = spawn_http_responses(vec![scroll_page(points)]);
    let reset = reset_synced_missing_versions(&fixture.context(url), None)?;
    let requests = handle.join().expect("qdrant test server");

    assert_eq!(reset, vec![reset_version.clone()]);
    assert!(!fixture.vectors_synced(&reset_version)?);
    assert_eq!(
        requests.len(),
        1,
        "the automatic repair scrolls once and never deletes"
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn explicit_file_repair_scopes_scroll_and_reset_to_its_files() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let (points, reset_version) = seed_reset_and_orphan(&mut fixture)?;
    // A second synced version outside the explicit file, also missing its point.
    let other_hash = "f".repeat(64);
    let other_version = fixture.version("src/other.rs", &other_hash, true)?;
    fixture.reference("src/other.rs", &other_hash)?;
    fixture.symbol("src/other.rs", &other_hash, "other1", "function", 0)?;

    let scope = vec!["src/reset.rs".to_string()];
    let (url, handle) = spawn_http_responses(vec![scroll_page(points)]);
    let reset = reset_synced_missing_versions(&fixture.context(url), Some(&scope))?;
    let requests = handle.join().expect("qdrant test server");

    assert_eq!(reset, vec![reset_version.clone()]);
    assert!(!fixture.vectors_synced(&reset_version)?);
    assert!(
        fixture.vectors_synced(&other_version)?,
        "a version outside the explicit files keeps its synced flag"
    );
    let scroll = requests
        .iter()
        .find(|request| request.contains("/points/scroll"))
        .expect("the repair scrolls Qdrant");
    let body = scroll.split("\r\n\r\n").nth(1).expect("scroll has a body");
    let body: Value = serde_json::from_str(body).expect("scroll body is JSON");
    let file_condition = body["filter"]["must"]
        .as_array()
        .expect("scoped scroll filters its conditions")
        .iter()
        .find(|condition| condition["key"] == "file_path")
        .expect("scoped scroll filters by file_path");
    assert_eq!(
        file_condition["match"]["any"],
        serde_json::json!(["src/reset.rs"])
    );
    assert!(
        delete_requests(&requests).is_empty(),
        "an explicit-file repair never deletes"
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_resets_only_synced_missing_versions() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let (mut points, reset_version) = seed_reset_and_orphan(&mut fixture)?;

    // Pending with a missing symbol point: its flag is already false and it is not reset.
    let pending_hash = "p".repeat(64);
    let pending = fixture.version("src/pending.rs", &pending_hash, false)?;
    fixture.reference("src/pending.rs", &pending_hash)?;
    fixture.symbol("src/pending.rs", &pending_hash, "p1", "function", 0)?;

    // Synced with every symbol point present.
    let complete_hash = "k".repeat(64);
    let complete = fixture.version("src/complete.rs", &complete_hash, true)?;
    fixture.reference("src/complete.rs", &complete_hash)?;
    let complete_symbol = fixture.symbol("src/complete.rs", &complete_hash, "k1", "function", 0)?;
    points.push((
        complete_symbol,
        payload("src/complete.rs", "k1", "function", 0),
    ));

    // Another project's synced-missing version keeps its flag.
    let mut other = Fixture::new()?;
    let other_hash = "o".repeat(64);
    let other_version = other.version("src/other.rs", &other_hash, true)?;
    other.reference("src/other.rs", &other_hash)?;
    other.symbol("src/other.rs", &other_hash, "o1", "function", 0)?;

    let digest = dry_run(&fixture, points.clone())?.inventory_digest;
    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let (url, handle) = spawn_http_responses(vec![scroll_page(points), delete_ok()]);
    let receipt = apply(&fixture.context(url), &digest, &receipt_path)?;
    handle.join().expect("qdrant test server");

    assert_eq!(receipt.planned_versions_reset, vec![reset_version.clone()]);
    assert_eq!(receipt.versions_reset, vec![reset_version.clone()]);
    assert!(!fixture.vectors_synced(&reset_version)?);
    assert!(
        !fixture.vectors_synced(&pending)?,
        "a pending version stays unsynced"
    );
    assert!(
        fixture.vectors_synced(&complete)?,
        "a complete version keeps its flag"
    );
    assert!(
        other.vectors_synced(&other_version)?,
        "another project's version keeps its flag"
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_makes_no_mutation_when_initial_receipt_write_fails() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let (points, version) = seed_reset_and_orphan(&mut fixture)?;
    let digest = dry_run(&fixture, points.clone())?.inventory_digest;

    // A private receipt directory the caller cannot write into.
    let parent = canonical_tempdir()?;
    let locked = parent.path().join("receipts");
    std::fs::create_dir(&locked)?;
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o500))?;
    let receipt_path = locked.join("reconcile-receipt.json");

    let (url, handle) = spawn_http_responses(vec![scroll_page(points)]);
    let error = apply(&fixture.context(url), &digest, &receipt_path)
        .expect_err("an unwritable receipt refuses the apply");
    let requests = handle.join().expect("qdrant test server");
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o700))?;

    let message = format!("{error:#}");
    assert!(
        message.contains(&receipt_path.display().to_string()),
        "the error names the receipt: {message}"
    );
    assert!(
        delete_requests(&requests).is_empty(),
        "no Qdrant delete is sent: {requests:?}"
    );
    assert!(fixture.vectors_synced(&version)?, "no flag is reset");
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_refuses_foreign_receipt_before_mutation() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let (points, version) = seed_reset_and_orphan(&mut fixture)?;
    let digest = dry_run(&fixture, points.clone())?.inventory_digest;

    // A receipt for the same project and collection but a different digest.
    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let foreign = ReconcileReceipt {
        digest: format!("sha256:{}", "f".repeat(64)),
        project_id: fixture.project_id.clone(),
        collection: code_symbols::collection_name(
            CODE_SYMBOL_COLLECTION_PREFIX,
            &fixture.project_id,
        )?,
        planned_orphans: BTreeMap::new(),
        planned_versions_reset: Vec::new(),
        versions_reset: Vec::new(),
        deleted: Vec::new(),
        complete: true,
    };
    std::fs::write(&receipt_path, serde_json::to_vec(&foreign)?)?;
    std::fs::set_permissions(&receipt_path, std::fs::Permissions::from_mode(0o600))?;

    let (url, handle) = spawn_http_responses(vec![scroll_page(points)]);
    let error = apply(&fixture.context(url), &digest, &receipt_path)
        .expect_err("a foreign receipt refuses the apply");
    let requests = handle.join().expect("qdrant test server");

    let message = format!("{error:#}");
    assert!(
        message.contains(&receipt_path.display().to_string()),
        "the error names the receipt: {message}"
    );
    assert!(
        delete_requests(&requests).is_empty(),
        "no Qdrant delete is sent: {requests:?}"
    );
    assert_eq!(
        receipt_on_disk(&receipt_path),
        foreign,
        "the foreign receipt is left as written"
    );
    assert!(fixture.vectors_synced(&version)?, "no flag is reset");
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_reset_survives_adoption_of_retained_version() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    // Synced, referenced by no machine, symbol absent: a retained version apply resets.
    let hash = "r".repeat(64);
    let retained = fixture.version("src/retained.rs", &hash, true)?;
    fixture.symbol("src/retained.rs", &hash, "r1", "function", 0)?;
    let points = orphan_points(1);
    let digest = dry_run(&fixture, points.clone())?.inventory_digest;

    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let (url, handle) = spawn_http_responses(vec![scroll_page(points), delete_ok()]);
    let receipt = apply(&fixture.context(url), &digest, &receipt_path)?;
    handle.join().expect("qdrant test server");
    assert_eq!(receipt.versions_reset, vec![retained.clone()]);

    // This machine later adopts the same content. The reset flag must block adoption.
    let adopted = crate::index::api::file_state::adopt_file_state(
        &mut fixture.conn,
        &fixture.machine_id,
        &fixture.project_id,
        "src/retained.rs",
        &hash,
        Path::new("/nonexistent/reconcile-adoption"),
        crate::index::api::IndexWriteMode::Overlay,
    )?;
    assert!(!adopted, "an unsynced version is not adopted");
    assert!(
        !fixture.vectors_synced(&retained)?,
        "the reset flag survives adoption"
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_stops_before_deletes_when_reset_receipt_write_fails() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let (points, version) = seed_reset_and_orphan(&mut fixture)?;
    let digest = dry_run(&fixture, points.clone())?.inventory_digest;

    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let (url, handle) = spawn_http_responses(vec![scroll_page(points)]);
    // Writes: 1 initial, 2 after the reset commit.
    let mut recorder = Recorder::failing(2, WriteFailure::BeforeRecord);
    let error = apply_with_persist(
        &fixture.context(url),
        &digest,
        &receipt_path,
        &mut |receipt| recorder.persist(receipt),
    )
    .expect_err("a failed reset receipt write refuses the run");
    let requests = handle.join().expect("qdrant test server");

    let message = format!("{error:#}");
    assert!(
        message.contains(&receipt_path.display().to_string()),
        "stderr names the receipt: {message}"
    );
    assert!(
        message.contains("versions_reset: 1"),
        "stderr gives the confirmed reset count: {message}"
    );
    assert!(
        delete_requests(&requests).is_empty(),
        "no delete follows the failed write: {requests:?}"
    );
    assert!(
        !fixture.vectors_synced(&version)?,
        "the committed reset stays"
    );
    assert_eq!(
        recorder.recorded.len(),
        1,
        "only the initial receipt was recorded"
    );
    let last = &recorder.recorded[0];
    assert!(
        last.versions_reset.is_empty() && last.deleted.is_empty() && !last.complete,
        "the last recorded receipt is the initial one: {last:?}",
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_stops_after_first_batch_when_receipt_write_fails() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let (mut points, version) = seed_reset_and_orphan(&mut fixture)?;
    // 257 orphans: batches of 256 and 1.
    points.extend(orphan_points(256));
    let digest = dry_run(&fixture, points.clone())?.inventory_digest;

    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let (url, handle) = spawn_http_responses(vec![scroll_page(points), delete_ok()]);
    // Writes: 1 initial, 2 after the reset, 3 after the first batch.
    let mut recorder = Recorder::failing(3, WriteFailure::BeforeRecord);
    let error = apply_with_persist(
        &fixture.context(url),
        &digest,
        &receipt_path,
        &mut |receipt| recorder.persist(receipt),
    )
    .expect_err("a failed batch receipt write refuses the run");
    let requests = handle.join().expect("qdrant test server");

    let deletes = delete_requests(&requests);
    assert_eq!(deletes.len(), 1, "no second batch is sent: {requests:?}");
    let message = format!("{error:#}");
    for id in deleted_ids(deletes[0]) {
        assert!(
            message.contains(&id),
            "stderr lists the first batch's IDs: {id}"
        );
    }
    let last = recorder
        .recorded
        .last()
        .expect("the initial and reset receipts were recorded");
    assert_eq!(last.versions_reset, vec![version]);
    assert!(
        last.deleted.is_empty(),
        "the batch is not recorded: {last:?}"
    );
    assert!(!last.complete, "the recorded receipt is partial");
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_exits_nonzero_when_final_receipt_write_fails() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let (points, _version) = seed_reset_and_orphan(&mut fixture)?;
    let digest = dry_run(&fixture, points.clone())?.inventory_digest;

    let receipt_dir = canonical_tempdir()?;
    let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
    let (url, handle) = spawn_http_responses(vec![scroll_page(points), delete_ok()]);
    // Writes: 1 initial, 2 after the reset, 3 after the batch, 4 final.
    let mut recorder = Recorder::failing(4, WriteFailure::BeforeRecord);
    let error = apply_with_persist(
        &fixture.context(url),
        &digest,
        &receipt_path,
        &mut |receipt| recorder.persist(receipt),
    )
    .expect_err("a failed final receipt write exits nonzero");
    let requests = handle.join().expect("qdrant test server");

    let deletes = delete_requests(&requests);
    assert_eq!(
        deletes.len(),
        1,
        "the batch was deleted before the final write"
    );
    let last = recorder
        .recorded
        .last()
        .expect("earlier receipts were recorded");
    assert!(!last.complete, "the last recorded receipt is not complete");
    assert_eq!(last.deleted, vec![deleted_ids(deletes[0])]);
    assert!(
        format!("{error:#}").contains(&receipt_path.display().to_string()),
        "the error names the receipt: {error:#}"
    );
    Ok(())
}

#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
#[test]
fn apply_reports_unconfirmed_when_published_receipt_write_fails() -> anyhow::Result<()> {
    // Each case publishes a receipt and then fails: the reset write (write 2) with no
    // delete after it, and the final write (write 4) after the single batch.
    for (write, expected_deletes) in [(2_usize, 0_usize), (4, 1)] {
        let mut fixture = Fixture::new()?;
        let (points, version) = seed_reset_and_orphan(&mut fixture)?;
        let digest = dry_run(&fixture, points.clone())?.inventory_digest;

        let receipt_dir = canonical_tempdir()?;
        let receipt_path = receipt_dir.path().join("reconcile-receipt.json");
        let mut responses = vec![scroll_page(points)];
        if expected_deletes > 0 {
            responses.push(delete_ok());
        }
        let (url, handle) = spawn_http_responses(responses);
        let mut recorder = Recorder::failing(write, WriteFailure::AfterRecord);
        let error = apply_with_persist(
            &fixture.context(url),
            &digest,
            &receipt_path,
            &mut |receipt| recorder.persist(receipt),
        )
        .expect_err("a failed receipt publish exits nonzero");
        let requests = handle.join().expect("qdrant test server");

        let message = format!("{error:#}");
        assert!(
            message.contains("unconfirmed"),
            "write {write}: the publish is reported unconfirmed: {message}"
        );
        assert_eq!(
            delete_requests(&requests).len(),
            expected_deletes,
            "write {write}: deletes sent: {requests:?}"
        );
        let last = recorder
            .recorded
            .last()
            .expect("the failing write recorded before it failed");
        if write == 4 {
            assert!(last.complete, "write 4 records a complete receipt");
        } else {
            assert_eq!(last.versions_reset, vec![version.clone()]);
            assert!(!last.complete, "write 2 records a partial receipt");
        }
        assert!(
            !fixture.vectors_synced(&version)?,
            "write {write}: the reset is committed"
        );
    }
    Ok(())
}
