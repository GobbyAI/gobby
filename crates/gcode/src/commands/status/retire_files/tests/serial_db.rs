use super::*;
use crate::config::{CodeVectorSettings, FalkorConfig, ProjectIndexScope, QdrantConfig};
use gobby_core::falkor::GraphClient;
use serde_json::json;

struct Fixture {
    extra_code_projects: Vec<uuid::Uuid>,
    interrupt_trigger: Option<String>,
    extra_machines: Vec<uuid::Uuid>,
    conn: Client,
    ctx: Context,
    manifest: Manifest,
    _directory: tempfile::TempDir,
    manifest_path: std::path::PathBuf,
    receipt_path: std::path::PathBuf,
    kept_id: String,
    kept_symbol: String,
}

impl Fixture {
    fn new() -> anyhow::Result<Self> {
        let database_url =
            crate::test_env::postgres_test_database_url("exact file retirement tests");
        let mut conn = db::connect_readwrite(&database_url)?;
        let directory = tempfile::tempdir()?;
        let parent = directory.path().canonicalize()?;
        let root = parent.join("repo");
        fs::create_dir(&root)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&parent, fs::Permissions::from_mode(0o700))?;
        }
        let mut manifest = sample_manifest(&root);
        manifest.machine_id = gobby_core::machine::read_local_machine_id()?;
        let project_id = db::id_param(&manifest.project_id)?;
        let machine_id = db::id_param(&manifest.machine_id)?;
        conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, $2)",
            &[&project_id, &format!("retire-{project_id}")],
        )?;
        conn.execute(
            "INSERT INTO code_indexed_projects (id) VALUES ($1)",
            &[&project_id],
        )?;
        conn.execute(
            "INSERT INTO project_checkouts (machine_id, project_id, root_path) VALUES ($1, $2, $3)",
            &[&machine_id, &project_id, &root.to_string_lossy().as_ref()],
        )?;
        conn.execute("INSERT INTO code_indexed_project_states (machine_id, project_id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms) VALUES ($1, $2, $3, 2, 2, NOW(), 0)", &[&machine_id, &project_id, &root.to_string_lossy().as_ref()])?;
        let retired = &manifest.files[0].versions[0];
        seed_content(
            &mut conn,
            &manifest.project_id,
            &manifest.files[0].file_path,
            &retired.id,
            &retired.content_hash,
            &retired.symbol_ids[0],
        )?;
        let kept_id = uuid::Uuid::new_v4().to_string();
        let kept_symbol = uuid::Uuid::new_v4().to_string();
        seed_content(
            &mut conn,
            &manifest.project_id,
            "src/keep.rs",
            &kept_id,
            &"c".repeat(64),
            &kept_symbol,
        )?;
        fs::create_dir(root.join("src"))?;
        fs::write(root.join("src/keep.rs"), "pub fn keep() {}")?;
        let ctx = Context {
            database_url,
            project_root: root,
            project_id: manifest.project_id.clone(),
            quiet: true,
            falkordb: Some(FalkorConfig {
                host: "127.0.0.1".to_string(),
                port: 1,
                password: None,
                graph_name: "gcode_retirement_test".to_string(),
            }),
            qdrant: Some(QdrantConfig {
                url: Some("http://127.0.0.1:1".to_string()),
                api_key: None,
            }),
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: ProjectIndexScope::Single,
        };
        manifest.backends = backend::BackendIdentity::from_context(&ctx)?;
        let fixture = Self {
            extra_code_projects: Vec::new(),
            interrupt_trigger: None,
            extra_machines: Vec::new(),
            conn,
            ctx,
            manifest,
            _directory: directory,
            manifest_path: parent.join("manifest.json"),
            receipt_path: parent.join("receipt.json"),
            kept_id,
            kept_symbol,
        };
        fixture.write_manifest()?;
        Ok(fixture)
    }

    fn write_manifest(&self) -> anyhow::Result<()> {
        fs::write(&self.manifest_path, serde_json::to_vec(&self.manifest)?)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&self.manifest_path, fs::Permissions::from_mode(0o600))?;
        }
        Ok(())
    }

    fn count(&mut self, id: &str) -> anyhow::Result<i64> {
        Ok(self
            .conn
            .query_one(
                "SELECT COUNT(*) FROM code_indexed_files WHERE id=$1",
                &[&db::id_param(id)?],
            )?
            .get(0))
    }

    fn enable_isolated_projections(&mut self) -> anyhow::Result<()> {
        let graph_url = reqwest::Url::parse(&std::env::var("GCODE_RETIRE_TEST_FALKOR_URL")?)?;
        let vector_url = reqwest::Url::parse(&std::env::var("GCODE_RETIRE_TEST_QDRANT_URL")?)?;
        for url in [&graph_url, &vector_url] {
            ensure!(
                url.host_str() == Some("127.0.0.1"),
                "test projections require explicit loopback endpoints"
            );
            ensure!(
                ![None, Some(6379), Some(6380), Some(6333)].contains(&url.port()),
                "production projection ports refused"
            );
        }
        self.ctx.falkordb = Some(FalkorConfig {
            host: "127.0.0.1".to_string(),
            port: graph_url.port().context("graph test port")?,
            password: graph_url.password().map(str::to_string),
            graph_name: "gcode_retirement_test".to_string(),
        });
        self.ctx.qdrant = Some(QdrantConfig {
            url: Some(vector_url.to_string().trim_end_matches('/').to_string()),
            api_key: None,
        });
        self.manifest.backends = backend::BackendIdentity::from_context(&self.ctx)?;
        self.write_manifest()?;
        Ok(())
    }

    fn graph(&self) -> anyhow::Result<GraphClient> {
        let config = self
            .ctx
            .falkordb
            .as_ref()
            .context("test graph configured")?;
        GraphClient::from_config(&config.connection_config(), &config.graph_name)
    }

    fn vector_collection_url(&self) -> anyhow::Result<String> {
        Ok(format!(
            "{}/collections/code_symbols_{}",
            self.ctx
                .qdrant
                .as_ref()
                .and_then(|config| config.url.as_ref())
                .context("test vector configured")?,
            self.manifest.project_id
        ))
    }

    fn seed_projections(&self) -> anyhow::Result<()> {
        let mut graph = self.graph()?;
        let retired = &self.manifest.files[0].versions[0];
        for (path, symbol, hash) in [
            (
                &self.manifest.files[0].file_path[..],
                &retired.symbol_ids[0][..],
                &retired.content_hash[..],
            ),
            ("src/keep.rs", &self.kept_symbol[..], &"c".repeat(64)[..]),
        ] {
            graph.query(&format!("CREATE (f:CodeFile {{project:'{}', path:'{path}'}})-[:DEFINES {{content_hash:'{hash}'}}]->(:CodeSymbol {{project:'{}',file_path:'{path}',file_content_hash:'{hash}',id:'{symbol}'}})", self.manifest.project_id, self.manifest.project_id), None)?;
        }
        let client = reqwest::blocking::Client::new();
        let url = self.vector_collection_url()?;
        client
            .put(&url)
            .json(&json!({"vectors":{"size":2,"distance":"Cosine"}}))
            .send()?
            .error_for_status()?;
        client.put(format!("{url}/points?wait=true")).json(&json!({"points":[
            {"id":retired.symbol_ids[0],"vector":[1.0,0.0],"payload":{"project_id":self.manifest.project_id,"file_path":self.manifest.files[0].file_path}},
            {"id":self.kept_symbol,"vector":[0.0,1.0],"payload":{"project_id":self.manifest.project_id,"file_path":"src/keep.rs"}}
        ]})).send()?.error_for_status()?;
        Ok(())
    }

    fn interrupt_sql_deletion(&mut self, enabled: bool) -> anyhow::Result<()> {
        if let Some(name) = self.interrupt_trigger.take() {
            self.conn.batch_execute(&format!(
                "DROP TRIGGER {name} ON code_indexed_files; DROP FUNCTION {name}()"
            ))?;
        }
        if enabled {
            let name = format!("retirement_interrupt_{}", uuid::Uuid::new_v4().simple());
            let id = &self.manifest.files[0].versions[0].id;
            self.conn.batch_execute(&format!("CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.id = '{id}'::uuid THEN RAISE EXCEPTION 'isolated retirement interruption'; END IF; RETURN OLD; END $$; CREATE TRIGGER {name} BEFORE DELETE ON code_indexed_files FOR EACH ROW EXECUTE FUNCTION {name}()"))?;
            self.interrupt_trigger = Some(name);
        }
        Ok(())
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.interrupt_sql_deletion(false)
            .expect("remove exact fixture interruption trigger");
        if let Ok(mut graph) = self.graph() {
            let _ = graph.query(
                &format!(
                    "MATCH (n {{project:'{}'}}) DETACH DELETE n",
                    self.manifest.project_id
                ),
                None,
            );
        }
        if let Ok(url) = self.vector_collection_url() {
            let _ = reqwest::blocking::Client::new().delete(url).send();
        }
        if let Ok(project_id) = db::id_param(&self.manifest.project_id) {
            for extra in &self.extra_code_projects {
                self.conn
                    .execute("DELETE FROM code_indexed_projects WHERE id=$1", &[extra])
                    .expect("remove exact synthetic fixture code project");
            }
            self.conn
                .execute(
                    "DELETE FROM code_indexed_file_states WHERE project_id=$1",
                    &[&project_id],
                )
                .expect("remove exact fixture selectors before content");
            self.conn
                .execute(
                    "DELETE FROM code_indexed_projects WHERE id=$1",
                    &[&project_id],
                )
                .expect("remove exact fixture code project");
            self.conn
                .execute("DELETE FROM projects WHERE id=$1", &[&project_id])
                .expect("remove exact fixture project");
            for machine in &self.extra_machines {
                self.conn
                    .execute("DELETE FROM machines WHERE id=$1", &[machine])
                    .expect("remove exact fixture machine");
            }
        }
    }
}

fn seed_content(
    conn: &mut Client,
    project: &str,
    path: &str,
    id: &str,
    hash: &str,
    symbol: &str,
) -> anyhow::Result<()> {
    let project = db::id_param(project)?;
    conn.execute("INSERT INTO code_indexed_files (id,project_id,file_path,language,content_hash,symbol_count,byte_size,graph_synced,vectors_synced,indexed_at,last_referenced_at) VALUES ($1,$2,$3,'rust',$4,1,20,true,true,NOW(),NOW())", &[&db::id_param(id)?, &project, &path, &hash])?;
    conn.execute("INSERT INTO code_symbols (id,project_id,file_path,name,qualified_name,kind,language,byte_start,byte_end,line_start,line_end,file_content_hash,content_hash,created_at,updated_at) VALUES ($1,$2,$3,'example','example','function','rust',0,20,1,1,$4,$4,NOW(),NOW())", &[&db::id_param(symbol)?, &project, &path, &hash])?;
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires isolated PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn exact_manifest_rejects_new_versions_symbols_and_machine_references() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    validate_context(&mut fixture.conn, &fixture.ctx, &fixture.manifest)?;
    let before = validate_file(
        &mut fixture.conn,
        &fixture.manifest,
        &fixture.manifest.files[0],
    )?;
    assert_eq!(before.len(), 1);
    let version_id = fixture.manifest.files[0].versions[0].id.clone();
    let unexpected_id = uuid::Uuid::new_v4().to_string();
    seed_content(
        &mut fixture.conn,
        &fixture.manifest.project_id,
        &fixture.manifest.files[0].file_path,
        &unexpected_id,
        &"d".repeat(64),
        &uuid::Uuid::new_v4().to_string(),
    )?;
    assert!(
        validate_file(
            &mut fixture.conn,
            &fixture.manifest,
            &fixture.manifest.files[0]
        )
        .is_err()
    );
    fixture.conn.execute(
        "DELETE FROM code_indexed_files WHERE id=$1",
        &[&db::id_param(&unexpected_id)?],
    )?;
    fixture.manifest.files[0].versions[0].symbol_ids.clear();
    assert!(
        validate_file(
            &mut fixture.conn,
            &fixture.manifest,
            &fixture.manifest.files[0]
        )
        .is_err()
    );
    fixture.manifest.files[0].versions[0].symbol_ids = before[0].symbol_ids.clone();
    let other_machine = uuid::Uuid::new_v4();
    fixture.conn.execute("INSERT INTO machines (id, hostname, owner_user_id) SELECT $1, 'retirement-fixture', owner_user_id FROM machines WHERE id=$2", &[&other_machine, &db::id_param(&fixture.manifest.machine_id)?])?;
    fixture.extra_machines.push(other_machine);
    fixture.conn.execute("INSERT INTO code_indexed_project_states (machine_id, project_id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms) VALUES ($1,$2,'/other-machine/checkout',1,1,NOW(),0)", &[&other_machine, &db::id_param(&fixture.manifest.project_id)?])?;
    fixture.conn.execute("INSERT INTO code_indexed_file_states (machine_id, project_id, file_path, content_hash) VALUES ($1,$2,$3,$4)", &[&other_machine, &db::id_param(&fixture.manifest.project_id)?, &fixture.manifest.files[0].file_path, &fixture.manifest.files[0].versions[0].content_hash])?;
    assert!(
        validate_file(
            &mut fixture.conn,
            &fixture.manifest,
            &fixture.manifest.files[0]
        )
        .is_err()
    );
    assert_eq!(fixture.count(&version_id)?, 1);
    assert_eq!(fixture.count(&fixture.kept_id.clone())?, 1);
    Ok(())
}

#[test]
fn exact_retirement_refuses_foreign_identity_and_checkout() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let correct_backends = fixture.manifest.backends.clone();
    fixture.manifest.backends.postgres_port += 1;
    assert!(validate_context(&mut fixture.conn, &fixture.ctx, &fixture.manifest).is_err());
    fixture.manifest.backends = correct_backends.clone();
    fixture.manifest.backends.qdrant_url = "http://127.0.0.1:2".to_string();
    assert!(validate_context(&mut fixture.conn, &fixture.ctx, &fixture.manifest).is_err());
    fixture.manifest.backends = correct_backends.clone();
    fixture.manifest.backends.falkor_graph = "unrelated".to_string();
    assert!(validate_context(&mut fixture.conn, &fixture.ctx, &fixture.manifest).is_err());
    fixture.manifest.backends = correct_backends;
    fixture.manifest.files[0].versions.push(ContentVersion {
        id: fixture.kept_id.clone(),
        content_hash: "c".repeat(64),
        symbol_ids: vec![fixture.kept_symbol.clone()],
    });
    assert!(
        validate_file(
            &mut fixture.conn,
            &fixture.manifest,
            &fixture.manifest.files[0]
        )
        .is_err()
    );
    fixture.manifest.files[0].versions.pop();
    fixture.manifest.files[0].versions[0]
        .symbol_ids
        .push(fixture.kept_symbol.clone());
    assert!(
        validate_file(
            &mut fixture.conn,
            &fixture.manifest,
            &fixture.manifest.files[0]
        )
        .is_err()
    );
    fixture.manifest.files[0].versions[0].symbol_ids.pop();
    let correct_project = fixture.ctx.project_id.clone();
    fixture.ctx.project_id = uuid::Uuid::new_v4().to_string();
    assert!(validate_context(&mut fixture.conn, &fixture.ctx, &fixture.manifest).is_err());
    fixture.ctx.project_id = correct_project;
    fixture.conn.execute(
        "DELETE FROM project_checkouts WHERE project_id=$1",
        &[&db::id_param(&fixture.manifest.project_id)?],
    )?;
    assert!(validate_context(&mut fixture.conn, &fixture.ctx, &fixture.manifest).is_err());
    assert_eq!(fixture.count(&fixture.kept_id.clone())?, 1);
    Ok(())
}

#[test]
fn exact_retirement_uses_shared_gc_lock_without_readonly_writes() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let mut lock_conn = db::connect_readwrite(&fixture.ctx.database_url)?;
    let _lock = lease_project_lock(
        &mut lock_conn,
        &fixture.ctx.project_id,
        IndexLockPolicy::maintenance_try(),
    )?
    .context("acquire fixture GC lock")?;
    run(&fixture.ctx, &fixture.manifest_path, false, None)?;
    let failure = run(
        &fixture.ctx,
        &fixture.manifest_path,
        true,
        Some(&fixture.receipt_path),
    )
    .expect_err("other lock holder must refuse apply");
    assert!(
        failure.to_string().contains("project index is busy"),
        "{failure:#}"
    );
    assert!(!fixture.receipt_path.exists());
    assert_eq!(
        fixture.count(&fixture.manifest.files[0].versions[0].id.clone())?,
        1
    );
    Ok(())
}

#[test]
fn exact_retirement_late_reference_preserves_content_and_requests_resync() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let candidate = validate_file(
        &mut fixture.conn,
        &fixture.manifest,
        &fixture.manifest.files[0],
    )?
    .remove(0);
    assert!(content_is_unreferenced(&mut fixture.conn, &candidate.id)?);
    fixture.conn.execute(
        "UPDATE code_indexed_files SET graph_synced=TRUE, vectors_synced=TRUE WHERE id=$1",
        &[&db::id_param(&candidate.id)?],
    )?;
    // Deterministically interleave a writer after admission/projection cleanup
    // but before the shared GC's final conditional SQL deletion.
    let mut writer = db::connect_readwrite(&fixture.ctx.database_url)?;
    writer.execute("INSERT INTO code_indexed_file_states (machine_id,project_id,file_path,content_hash) VALUES ($1,$2,$3,$4)", &[&db::id_param(&fixture.manifest.machine_id)?, &db::id_param(&fixture.manifest.project_id)?, &candidate.file_path, &candidate.content_hash])?;
    assert!(!delete_unreferenced_content_row(
        &mut fixture.conn,
        &candidate.id
    )?);
    let row = fixture.conn.query_one(
        "SELECT graph_synced, vectors_synced FROM code_indexed_files WHERE id=$1",
        &[&db::id_param(&candidate.id)?],
    )?;
    assert!(!row.get::<_, bool>(0));
    assert!(!row.get::<_, bool>(1));
    assert_eq!(fixture.count(&candidate.id)?, 1);
    assert_eq!(fixture.count(&fixture.kept_id.clone())?, 1);
    Ok(())
}

#[test]
fn exact_retirement_admits_registered_synthetic_worktree_without_fake_checkout()
-> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    let git = |args: &[&str]| -> anyhow::Result<()> {
        let output = std::process::Command::new("git")
            .arg("-C")
            .arg(&fixture.manifest.root_path)
            .args(args)
            .output()?;
        ensure!(
            output.status.success(),
            "fixture Git failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        Ok(())
    };
    git(&["init", "-q"])?;
    git(&[
        "-c",
        "user.name=Retirement Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "isolated fixture",
    ])?;
    let root = fixture
        .manifest
        .root_path
        .parent()
        .context("fixture parent")?
        .join("linked-worktree");
    git(&[
        "worktree",
        "add",
        "--detach",
        root.to_str().context("fixture root UTF-8")?,
    ])?;
    let identity = crate::config::resolve_project_identity(&root)?;
    assert!(matches!(
        identity.source,
        crate::config::ProjectIdentitySource::LinkedWorktree
    ));
    fs::create_dir(fixture.manifest.root_path.join(".gobby"))?;
    fs::write(
        fixture.manifest.root_path.join(".gobby/project.json"),
        serde_json::to_vec(&json!({"id":fixture.manifest.project_id}))?,
    )?;
    fixture.conn.execute(
        "DELETE FROM project_checkouts WHERE project_id=$1",
        &[&db::id_param(&fixture.manifest.project_id)?],
    )?;
    assert!(
        validate_context(&mut fixture.conn, &fixture.ctx, &fixture.manifest).is_err(),
        "ordinary project identity must not substitute an indexed root for its missing primary checkout"
    );
    let mut isolated = sample_manifest(&root);
    isolated.project_id = identity.project_id;
    isolated.machine_id = fixture.manifest.machine_id.clone();
    isolated.backends = fixture.manifest.backends.clone();
    fixture.ctx.project_id = isolated.project_id.clone();
    fixture.ctx.project_root = root;
    let id = db::id_param(&isolated.project_id)?;
    fixture
        .conn
        .execute("INSERT INTO code_indexed_projects (id) VALUES ($1)", &[&id])?;
    fixture.extra_code_projects.push(id);
    assert!(validate_context(&mut fixture.conn, &fixture.ctx, &isolated).is_err());
    fixture.conn.execute("INSERT INTO code_indexed_project_states (machine_id,project_id,root_path,total_files,total_symbols,last_indexed_at,index_duration_ms) VALUES ($1,$2,$3,0,0,NOW(),0)", &[&db::id_param(&isolated.machine_id)?, &id, &isolated.root_path.to_string_lossy().as_ref()])?;
    validate_context(&mut fixture.conn, &fixture.ctx, &isolated)?;
    let rows: i64 = fixture
        .conn
        .query_one(
            "SELECT COUNT(*) FROM project_checkouts WHERE project_id=$1",
            &[&id],
        )?
        .get(0);
    assert_eq!(
        rows, 0,
        "synthetic admission must not manufacture a primary checkout"
    );
    fixture.conn.execute(
        "UPDATE code_indexed_project_states SET root_path='/wrong-root' WHERE project_id=$1",
        &[&id],
    )?;
    assert!(validate_context(&mut fixture.conn, &fixture.ctx, &isolated).is_err());
    Ok(())
}

#[test]
#[ignore = "requires explicit isolated PostgreSQL, Qdrant and FalkorDB endpoints"]
#[serial_test::serial(serial_db)]
fn exact_retirement_roundtrip_retries_interrupted_sql_deletion() -> anyhow::Result<()> {
    let mut fixture = Fixture::new()?;
    fixture.enable_isolated_projections()?;
    fixture.seed_projections()?;
    // Content without remaining SQL facts can retain an exact import edge;
    // its shared module endpoint must survive the bounded cleanup.
    let content_id = uuid::Uuid::new_v4().to_string();
    let removed_symbol = uuid::Uuid::new_v4().to_string();
    let content_hash = "d".repeat(64);
    seed_content(
        &mut fixture.conn,
        &fixture.manifest.project_id,
        "wiki/content.md",
        &content_id,
        &content_hash,
        &removed_symbol,
    )?;
    fixture.conn.execute(
        "DELETE FROM code_symbols WHERE id=$1",
        &[&db::id_param(&removed_symbol)?],
    )?;
    fixture.manifest.files.push(RetiredFile {
        file_path: "wiki/content.md".to_string(),
        versions: vec![ContentVersion {
            id: content_id.clone(),
            content_hash: content_hash.clone(),
            symbol_ids: vec![],
        }],
    });
    fixture.graph()?.query(&format!("CREATE (:CodeFile {{project:'{}',path:'wiki/content.md'}})-[:IMPORTS {{content_hash:'{content_hash}'}}]->(:CodeModule {{project:'{}',id:'keep-module',name:'keep-module'}})", fixture.manifest.project_id, fixture.manifest.project_id), None)?;
    fixture.write_manifest()?;
    let id = fixture.manifest.files[0].versions[0].id.clone();
    let symbol = fixture.manifest.files[0].versions[0].symbol_ids[0].clone();
    run(&fixture.ctx, &fixture.manifest_path, false, None)?;
    assert!(
        !fixture.receipt_path.exists(),
        "validation must not create a receipt"
    );
    assert_eq!(fixture.count(&id)?, 1);
    let unknown = uuid::Uuid::new_v4().to_string();
    fixture.graph()?.query(&format!("CREATE (:CodeSymbol {{project:'{}', file_path:'wiki/page.md', file_content_hash:'{}', id:'{unknown}'}})", fixture.manifest.project_id, "f".repeat(64)), None)?;
    assert!(
        run(
            &fixture.ctx,
            &fixture.manifest_path,
            true,
            Some(&fixture.receipt_path)
        )
        .is_err()
    );
    assert!(
        !fixture.receipt_path.exists(),
        "graph admission must precede any cleanup"
    );
    assert_eq!(fixture.count(&id)?, 1);
    fixture.graph()?.query(
        &format!(
            "MATCH (s:CodeSymbol {{project:'{}', id:'{unknown}'}}) DELETE s",
            fixture.manifest.project_id
        ),
        None,
    )?;
    fixture.graph()?.query(&format!("MATCH (s:CodeSymbol {{project:'{}',id:'{}'}}) CREATE (s)-[:CALLS {{source_file_path:'wiki/page.md',content_hash:'{}'}}]->(s)", fixture.manifest.project_id, fixture.kept_symbol, fixture.manifest.files[0].versions[0].content_hash), None)?;
    assert!(
        run(
            &fixture.ctx,
            &fixture.manifest_path,
            true,
            Some(&fixture.receipt_path)
        )
        .is_err(),
        "detached source-file-only CALLS cannot be silently left behind"
    );
    assert!(!fixture.receipt_path.exists());
    assert_eq!(fixture.count(&id)?, 1);
    fixture.graph()?.query(&format!("MATCH (s:CodeSymbol {{project:'{}',id:'{}'}})-[r:CALLS]->(s) WHERE r.source_file_path='wiki/page.md' DELETE r", fixture.manifest.project_id, fixture.kept_symbol), None)?;
    fixture.interrupt_sql_deletion(true)?;
    assert!(
        run(
            &fixture.ctx,
            &fixture.manifest_path,
            true,
            Some(&fixture.receipt_path)
        )
        .is_err()
    );
    let partial: Receipt = serde_json::from_slice(&fs::read(&fixture.receipt_path)?)?;
    assert!(!partial.complete);
    assert_eq!(
        fixture.count(&id)?,
        1,
        "interruption after projections must preserve recoverable SQL content"
    );
    assert_eq!(fixture.count(&fixture.kept_id.clone())?, 1);
    fixture.interrupt_sql_deletion(false)?;
    run(
        &fixture.ctx,
        &fixture.manifest_path,
        true,
        Some(&fixture.receipt_path),
    )?;
    run(
        &fixture.ctx,
        &fixture.manifest_path,
        true,
        Some(&fixture.receipt_path),
    )?;
    assert_eq!(fixture.count(&id)?, 0);
    assert_eq!(fixture.count(&content_id)?, 0);
    assert_eq!(fixture.count(&fixture.kept_id.clone())?, 1);
    assert_eq!(
        fs::read_to_string(fixture.ctx.project_root.join("src/keep.rs"))?,
        "pub fn keep() {}"
    );
    let rows = fixture.graph()?.query(
        &format!(
            "MATCH (n {{project:'{}'}}) RETURN count(n) AS count",
            fixture.manifest.project_id
        ),
        None,
    )?;
    assert_eq!(
        rows[0]["count"],
        json!(3),
        "unrelated graph file/symbol and shared module must survive"
    );
    let points: serde_json::Value = reqwest::blocking::Client::new()
        .post(format!("{}/points", fixture.vector_collection_url()?))
        .json(&json!({"ids":[symbol, fixture.kept_symbol]}))
        .send()?
        .error_for_status()?
        .json()?;
    assert_eq!(
        points["result"].as_array().context("point result")?.len(),
        1
    );
    assert_eq!(points["result"][0]["id"], fixture.kept_symbol);
    let completed: Receipt = serde_json::from_slice(&fs::read(&fixture.receipt_path)?)?;
    assert!(completed.complete);
    // A completed receipt never admits a new content version on retry.
    seed_content(
        &mut fixture.conn,
        &fixture.manifest.project_id,
        &fixture.manifest.files[0].file_path,
        &uuid::Uuid::new_v4().to_string(),
        &"e".repeat(64),
        &uuid::Uuid::new_v4().to_string(),
    )?;
    assert!(
        run(
            &fixture.ctx,
            &fixture.manifest_path,
            true,
            Some(&fixture.receipt_path)
        )
        .is_err()
    );
    Ok(())
}
