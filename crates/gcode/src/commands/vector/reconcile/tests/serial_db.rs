use super::super::{Inventory, MissingClass, MissingGroup, OrphanClass, inventory, inventory_digest};
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope, QdrantConfig};
use crate::db;
use crate::models::{CODE_INDEX_UUID_NAMESPACE, Symbol};
use crate::vector::code_symbols::tests::spawn_http_responses;
use serde_json::{Value, json};
use std::path::PathBuf;
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
    fn version(&mut self, file_path: &str, content_hash: &str, synced: bool) -> anyhow::Result<String> {
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
        let id = Symbol::make_id(&self.project_id, file_path, content_hash, name, kind, byte_start);
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
            let column = if table == "projects" { "id" } else { "project_id" };
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
        (legacy_scheme_id.clone(), payload("src/gone.rs", "fn_a", "function", 10)),
        (reparsed_id.clone(), payload(old_path, "fn_b", "function", 20)),
        (unattributed_id.clone(), payload("src/none.rs", "x", "function", 1)),
    ])]);
    let report = inventory(&fixture.context(url))?;
    handle.join().expect("qdrant test server");

    assert_eq!(report.points_scanned, 6);
    assert_eq!(report.pg_symbols, 2);
    assert_eq!(report.orphans[&OrphanClass::LegacyPayload], vec![legacy_payload_id.clone()]);
    assert_eq!(report.orphans[&OrphanClass::LegacyIdScheme], vec![legacy_scheme_id.clone()]);
    assert_eq!(report.orphans[&OrphanClass::ReparsedVersion], vec![reparsed_id.clone()]);
    assert_eq!(report.orphans[&OrphanClass::Unattributed], vec![unattributed_id.clone()]);
    assert_eq!(
        report.inventory_digest,
        inventory_digest(
            &[legacy_payload_id, legacy_scheme_id, reparsed_id, unattributed_id],
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
    assert!(fixture.vectors_synced(&version)?, "synced flag must stay true");
    assert!(!fixture.vectors_synced(&pending)?, "pending flag must stay false");
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
