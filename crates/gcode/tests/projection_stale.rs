#[path = "common/pin_collaborator.rs"]
mod pin_collaborator;

#[cfg(test)]
mod serial_db {
    use postgres::{Client, NoTls};
    use serde_json::Value;
    use std::path::{Path, PathBuf};
    use std::process::Command;

    use gobby_code::test_env;

    // UUIDv5(CODE_INDEX_UUID_NAMESPACE, "gcode-projection-stale-missing-file"):
    // the hub stores project ids as native uuid.
    const PROJECT_ID: &str = "4424ae52-9ce0-52ae-af19-8c3a093c351f";
    const FILE_PATH: &str = "src/lib.rs";
    const CODE_INDEX_UUID_NAMESPACE: uuid::Uuid = uuid::Uuid::from_bytes([
        0xc0, 0xde, 0x1d, 0xe0, 0x00, 0x00, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00,
    ]);

    #[test]
    fn projection_fixture_namespace_is_canonical() {
        assert_eq!(
            CODE_INDEX_UUID_NAMESPACE.to_string(),
            "c0de1de0-0000-4000-8000-000000000000"
        );
    }

    /// Parse a fixture id for binding against native-uuid hub columns.
    fn uuid_param(id: &str) -> uuid::Uuid {
        uuid::Uuid::parse_str(id).expect("fixture id is a uuid")
    }

    fn local_machine_uuid() -> uuid::Uuid {
        uuid_param(&gobby_core::machine::read_local_machine_id().expect("read local machine id"))
    }

    fn isolated_gobby_home(root: &Path) -> PathBuf {
        let home = root.join(".no-daemon-home");
        std::fs::create_dir_all(&home).expect("create isolated Gobby home");
        std::fs::write(home.join("machine_id"), local_machine_uuid().to_string())
            .expect("write isolated machine id");
        home
    }

    fn attach_managed_grant(
        command: &mut Command,
        home: &Path,
        project_id: &str,
        connections: gobby_core::grant::DirectConnections,
    ) {
        let machine = std::fs::read_to_string(home.join("machine_id")).expect("read machine id");
        let grant =
            gobby_core::grant::managed_direct_grant(project_id, machine.trim(), &connections);
        let path = gobby_core::grant::write_managed_bootstrap(&home.join("grants"), &grant)
            .expect("write managed grant");
        // The grant fixture's execution id is fixed; an inherited managed-session
        // run id would fail the identity check before the scope under test runs.
        command
            .env_remove("GOBBY_AGENT_RUN_ID")
            .env_remove("GOBBY_MANAGED_EXECUTION_ID")
            .env("GOBBY_HOME", home)
            .env("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", path);
    }

    /// Deterministic uuid for a seeded child row of `project_id`.
    fn row_uuid(project_id: &str, label: &str) -> uuid::Uuid {
        uuid::Uuid::new_v5(
            &CODE_INDEX_UUID_NAMESPACE,
            format!("{project_id}:{label}").as_bytes(),
        )
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn vector_sync_file_allows_deleted_local_file_state() {
        let database_url = test_env::postgres_test_database_url("projection stale tests");
        let mut conn = Client::connect(&database_url, NoTls).expect("connect PostgreSQL");
        cleanup_project(&mut conn, PROJECT_ID).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: PROJECT_ID.to_string(),
        };

        let project = tempfile::tempdir().expect("temp project");
        std::fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
        std::fs::create_dir_all(project.path().join("src")).expect("create src");
        std::fs::write(project.path().join(FILE_PATH), "pub fn indexed() {}\n")
            .expect("write source file");
        // gcode resolves only Gobby-registered identities; a standalone
        // gcode.json would fail with checkout_required before the command runs.
        std::fs::write(
            project.path().join(".gobby/project.json"),
            serde_json::json!({
                "id": PROJECT_ID,
                "name": "projection-stale"
            })
            .to_string(),
        )
        .expect("write project identity");

        seed_indexed_file(&mut conn, PROJECT_ID, FILE_PATH);
        let machine_id = local_machine_uuid();
        conn.execute(
            "DELETE FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2 AND file_path = $3",
            &[&machine_id, &uuid_param(PROJECT_ID), &FILE_PATH],
        )
        .expect("delete local indexed file state");

        // The managed grant's DSN role must hold exactly one live principal binding
        // for the project; the hub's RLS helpers raise on zero or duplicate rows.
        test_env::seed_test_principal_binding(&mut conn, PROJECT_ID)
            .expect("bind managed principal to the fixture project");

        let home = isolated_gobby_home(project.path());
        let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
        command
            .current_dir(project.path())
            .arg("--allow-stale")
            .arg("--format")
            .arg("json")
            .args([
                "vector",
                "sync-file",
                "--file",
                FILE_PATH,
                "--allow-missing-indexed-file",
            ]);
        attach_managed_grant(
            &mut command,
            &home,
            PROJECT_ID,
            gobby_core::grant::DirectConnections::postgres(&database_url),
        );
        let output = command.output().expect("run gcode vector sync-file");

        assert!(
            output.status.success(),
            "sync-file should skip missing rows, stderr={}",
            String::from_utf8_lossy(&output.stderr)
        );
        let payload: Value =
            serde_json::from_slice(&output.stdout).expect("sync-file output is JSON");
        assert_eq!(payload["status"], "skipped");
        assert_eq!(payload["reason"], "indexed_file_not_found");
        assert_eq!(payload["skipped_files"], 1);
        assert_eq!(payload["failed_files"], 0);
        assert!(
            !String::from_utf8_lossy(&output.stderr).contains("indexed file was not found"),
            "missing-row tolerance must not emit the old hard failure"
        );
    }

    struct ProjectCleanup {
        database_url: String,
        project_id: String,
    }

    impl Drop for ProjectCleanup {
        fn drop(&mut self) {
            if let Ok(mut conn) = Client::connect(&self.database_url, NoTls) {
                let _ = cleanup_project(&mut conn, &self.project_id);
            }
        }
    }

    fn seed_indexed_file(conn: &mut Client, project_id: &str, file_path: &str) {
        seed_project_with_root(conn, project_id, "/tmp/projection-stale");
        seed_indexed_file_without_project(conn, project_id, file_path);
        seed_file_state(conn, project_id, file_path);
    }

    fn seed_project_with_root(conn: &mut Client, project_id: &str, root_path: impl AsRef<str>) {
        let project_id = uuid_param(project_id);
        let machine_id = local_machine_uuid();
        conn.execute(
            "INSERT INTO code_indexed_projects (id) VALUES ($1)",
            &[&project_id],
        )
        .expect("insert indexed project identity");
        conn.execute(
            "INSERT INTO code_indexed_project_states
                (machine_id, project_id, root_path, total_files, total_symbols,
                 last_indexed_at, index_duration_ms)
             VALUES ($1, $2, $3, 1, 1, NOW(), 0)",
            &[&machine_id, &project_id, &root_path.as_ref()],
        )
        .expect("insert indexed project state");
    }

    fn seed_indexed_file_without_project(conn: &mut Client, project_id: &str, file_path: &str) {
        let project_uuid = uuid_param(project_id);
        conn.execute(
            "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
             VALUES ($1, $2, $3, 'rust', 'hash-1', 1, 19, false, true, NULL, NOW())",
            &[&row_uuid(project_id, "file"), &project_uuid, &file_path],
        )
        .expect("insert indexed file");
        conn.execute(
            "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 file_content_hash, content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, 'indexed', 'crate::indexed', 'function', 'rust', 0, 19,
                 1, 1, 'pub fn indexed()', NULL, NULL, 'hash-1', 'hash-1', NULL, NOW(), NOW())",
            &[&row_uuid(project_id, "symbol"), &project_uuid, &file_path],
        )
        .expect("insert symbol");
    }

    fn seed_file_state(conn: &mut Client, project_id: &str, file_path: &str) {
        let machine_id = local_machine_uuid();
        conn.execute(
            "INSERT INTO code_indexed_file_states
                (machine_id, project_id, file_path, content_hash)
             VALUES ($1, $2, $3, 'hash-1')",
            &[&machine_id, &uuid_param(project_id), &file_path],
        )
        .expect("insert indexed file state");
    }

    fn cleanup_project(conn: &mut Client, project_id: &str) -> anyhow::Result<()> {
        let project_id = uuid_param(project_id);
        conn.execute(
            "DELETE FROM code_overlay_pins WHERE overlay_project_id = $1 OR source_project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM gobby_agent_auth.principal_bindings WHERE project_id = $1",
            &[&project_id],
        )?;
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
        Ok(())
    }

    struct PinnedCli {
        root: tempfile::TempDir,
        home: PathBuf,
        overlay: String,
        service: super::pin_collaborator::Collaborator,
        _overlay_cleanup: ProjectCleanup,
        _source_cleanup: ProjectCleanup,
    }

    impl PinnedCli {
        fn new(conn: &mut Client, database_url: &str) -> Self {
            use gobby_core::grant::{
                AiCapability, DirectConnections, PrincipalKind, managed_direct_grant,
            };
            let root = tempfile::tempdir().expect("isolated child checkout");
            let parent = root.path().join("parent");
            std::fs::create_dir_all(&parent).expect("parent root");
            std::fs::create_dir_all(root.path().join(".gobby")).expect("child metadata");
            let source = uuid::Uuid::new_v5(
                &CODE_INDEX_UUID_NAMESPACE,
                root.path().to_str().expect("root").as_bytes(),
            )
            .to_string();
            let overlay = gobby_core::project::code_index_id_for_root(root.path());
            std::fs::write(
                root.path().join(".gobby/isolation.json"),
                serde_json::json!({
                    "parent_project_id":source, "parent_project_path":parent,
                })
                .to_string(),
            )
            .expect("overlay identity");
            seed_project_with_root(conn, &source, parent.to_str().expect("parent path"));
            seed_indexed_file_without_project(conn, &source, FILE_PATH);
            seed_file_state(conn, &source, FILE_PATH);
            seed_project_with_root(conn, &overlay, root.path().to_str().expect("child path"));
            conn.execute("INSERT INTO code_indexed_files (id,project_id,file_path,language,content_hash,symbol_count,byte_size,indexed_at)
                VALUES ($1,$2,$3,'rust','hash-2',0,0,NOW())", &[&row_uuid(&source,"new-file"),&uuid_param(&source),&FILE_PATH]).expect("new source content");
            conn.execute(
                "UPDATE code_indexed_file_states SET content_hash='hash-2' WHERE project_id=$1",
                &[&uuid_param(&source)],
            )
            .expect("source selector moves");
            conn.execute("INSERT INTO code_overlay_pins (machine_id,overlay_project_id,file_path,source_project_id,content_hash)
                VALUES ($1,$2,$3,$4,'hash-1')", &[&local_machine_uuid(),&uuid_param(&overlay),&FILE_PATH,&uuid_param(&source)]).expect("pin old source");
            let home = isolated_gobby_home(root.path());
            std::fs::write(
                home.join("bootstrap.yaml"),
                format!("api_key: {}\n", super::pin_collaborator::TOKEN),
            )
            .expect("test API key");
            let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind collaborator");
            let url = format!("http://{}", listener.local_addr().expect("service address"));
            let mut grant = managed_direct_grant(
                &source,
                &local_machine_uuid().to_string(),
                &DirectConnections::postgres(database_url).with_qdrant(&url, None),
            );
            grant.principal.kind = PrincipalKind::Interactive;
            grant.principal.execution_id = None;
            grant.principal.session_id = None;
            grant.principal.code_overlay_project_id = Some(overlay.clone());
            grant.capabilities.embed = AiCapability::Daemon {};
            grant.deployment.token = gobby_core::grant::deployment_token(&home);
            let service = super::pin_collaborator::Collaborator::start(
                listener,
                grant.with_checksum(),
                row_uuid(&source, "symbol").to_string(),
            );
            Self {
                root,
                home,
                overlay: overlay.clone(),
                service,
                _overlay_cleanup: ProjectCleanup {
                    database_url: database_url.into(),
                    project_id: overlay.clone(),
                },
                _source_cleanup: ProjectCleanup {
                    database_url: database_url.into(),
                    project_id: source,
                },
            }
        }

        fn run(&self, args: &[&str]) -> std::process::Output {
            Command::new(env!("CARGO_BIN_EXE_gcode"))
                .current_dir(self.root.path())
                .args(["--allow-stale", "--format", "json"])
                .args(args)
                .env("GOBBY_HOME", &self.home)
                .env("GOBBY_DAEMON_URL", &self.service.url)
                .env_remove("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
                .env_remove("GOBBY_SESSION_ID")
                .env_remove("GOBBY_PARENT_SESSION_ID")
                .env_remove("GOBBY_AGENT_RUN_ID")
                .env_remove("GOBBY_MANAGED_EXECUTION_ID")
                .env_remove("GOBBY_AGENT_API_TOKEN")
                .output()
                .expect("run isolated gcode")
        }
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    fn pinned_sync_file_routes_through_cli() {
        let database_url = test_env::postgres_test_database_url("pinned CLI routes");
        let mut conn = Client::connect(&database_url, NoTls).expect("test PostgreSQL");
        let fixture = PinnedCli::new(&mut conn, &database_url);
        let before: String = conn.query_one("SELECT jsonb_agg(to_jsonb(f) ORDER BY id)::text FROM code_indexed_files f WHERE project_id=$1", &[&uuid_param(&fixture._source_cleanup.project_id)]).expect("source snapshot").get(0);
        let vector = fixture.run(&[
            "vector",
            "sync-file",
            "--file",
            FILE_PATH,
            "--allow-missing-indexed-file",
        ]);
        let graph = fixture.run(&[
            "graph",
            "sync-file",
            "--file",
            FILE_PATH,
            "--allow-missing-indexed-file",
        ]);
        let missing = fixture.run(&[
            "vector",
            "sync-file",
            "--file",
            "untracked.rs",
            "--allow-missing-indexed-file",
        ]);
        let pin = conn.query_one("SELECT vector_projected,graph_projected,graph_attempted_at IS NOT NULL FROM code_overlay_pins WHERE overlay_project_id=$1", &[&uuid_param(&fixture.overlay)]).expect("pin recovery state");
        let after: String = conn.query_one("SELECT jsonb_agg(to_jsonb(f) ORDER BY id)::text FROM code_indexed_files f WHERE project_id=$1", &[&uuid_param(&fixture._source_cleanup.project_id)]).expect("source after recovery").get(0);
        assert!(
            vector.status.success(),
            "vector: {}",
            String::from_utf8_lossy(&vector.stderr)
        );
        let output: Value = serde_json::from_slice(&vector.stdout).expect("vector JSON");
        assert_ne!(
            output["status"], "skipped",
            "pinned path must recover: {output}"
        );
        assert!(
            pin.get::<_, bool>(0),
            "vector flag marks completed pinned recovery"
        );
        assert!(
            pin.get::<_, bool>(2),
            "graph helper records attempt, stderr={}",
            String::from_utf8_lossy(&graph.stderr)
        );
        assert!(
            !pin.get::<_, bool>(1),
            "graph backend is unavailable in this isolated fixture"
        );
        assert_eq!(
            before, after,
            "source flags and attempt timestamps stay untouched"
        );
        assert!(
            missing.status.success(),
            "missing path stderr={}",
            String::from_utf8_lossy(&missing.stderr)
        );
        let skipped: Value = serde_json::from_slice(&missing.stdout).expect("missing JSON");
        assert_eq!(skipped["reason"], "indexed_file_not_found");
        assert_eq!(skipped["skipped_files"], 1);
        let requests = fixture.service.requests.lock().expect("request log");
        let child_collection = gobby_core::qdrant::collection_name(
            "code_symbols_",
            gobby_core::qdrant::CollectionScope::Project(&fixture.overlay),
        )
        .expect("child collection");
        assert!(
            requests.iter().any(|request| request.starts_with("PUT ")
                && request.contains(&child_collection)
                && request.contains(
                    &row_uuid(&fixture._source_cleanup.project_id, "symbol").to_string()
                )),
            "pinned source symbol must be written into child namespace"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    fn text_search_survives_projection_recovery() {
        let database_url = test_env::postgres_test_database_url("pinned text availability");
        let mut conn = Client::connect(&database_url, NoTls).expect("test PostgreSQL");
        let fixture = PinnedCli::new(&mut conn, &database_url);
        let output = fixture.run(&["search", "indexed"]);
        assert!(
            output.status.success(),
            "search: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let payload: Value = serde_json::from_slice(&output.stdout).expect("search JSON");
        assert!(
            payload
                .to_string()
                .contains(&row_uuid(&fixture._source_cleanup.project_id, "symbol").to_string()),
            "pinned PostgreSQL/BM25 hit remains visible: {payload}"
        );
        assert!(
            payload["warnings"]
                .as_array()
                .is_some_and(|warnings| warnings.iter().any(|warning| {
                    let text = warning.to_string().to_lowercase();
                    (text.contains("projection") || text.contains("pin"))
                        && ["pending", "recover", "unavailable", "degraded"]
                            .iter()
                            .any(|state| text.contains(state))
                })),
            "search must report pending projection degradation: {payload}"
        );
    }
}
