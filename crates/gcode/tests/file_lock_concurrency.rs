#[cfg(test)]
mod serial_db {
    use postgres::{Client, NoTls};
    use serde_json::Value;
    use sha2::{Digest, Sha256};
    use std::path::{Path, PathBuf};
    use std::process::{Command, Output};

    use gobby_code::test_env;

    const CODE_INDEX_UUID_NAMESPACE: uuid::Uuid = uuid::Uuid::from_bytes([
        0xc0, 0xde, 0x1d, 0xe0, 0x00, 0x00, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00,
    ]);

    struct TestProject {
        root: tempfile::TempDir,
        home: PathBuf,
        database_url: String,
        project_id: String,
    }

    impl TestProject {
        fn new(label: &str) -> Self {
            let database_url = test_env::postgres_test_database_url("file lock CLI tests");
            let project_id = uuid::Uuid::new_v5(
                &CODE_INDEX_UUID_NAMESPACE,
                format!("gcode-file-lock-cli:{label}").as_bytes(),
            )
            .to_string();
            let mut conn = Client::connect(&database_url, NoTls).expect("connect PostgreSQL");
            cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");

            let root = tempfile::tempdir().expect("create temp project");
            std::fs::create_dir_all(root.path().join(".gobby")).expect("create .gobby");
            std::fs::create_dir_all(root.path().join("src")).expect("create src");
            std::fs::write(root.path().join("src/held.rs"), "pub fn held() {}\n")
                .expect("write held file");
            std::fs::write(root.path().join("src/free.rs"), "pub fn free() {}\n")
                .expect("write free file");
            std::fs::write(
                root.path().join(".gobby/project.json"),
                serde_json::json!({"id": project_id, "name": label}).to_string(),
            )
            .expect("write project identity");
            let git_status = Command::new("git")
                .arg("-C")
                .arg(root.path())
                .args(["init", "--quiet"])
                .status()
                .expect("run git init");
            assert!(git_status.success(), "git init should succeed");
            test_env::seed_test_checkout(&mut conn, &project_id, root.path())
                .expect("seed test checkout");

            let home = isolated_gobby_home(root.path());
            Self {
                root,
                home,
                database_url,
                project_id,
            }
        }

        fn run_index(&self, format: &str, full: bool, files: &[&str]) -> Output {
            let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
            command
                .current_dir(self.root.path())
                .arg("index")
                .arg("--project")
                .arg(self.root.path())
                .arg("--quiet")
                .arg("--skip-if-locked")
                .arg("--format")
                .arg(format);
            if full {
                command.arg("--full");
            }
            if !files.is_empty() {
                command.arg("--files").args(files);
            }
            attach_managed_grant(
                &mut command,
                &self.home,
                &self.project_id,
                gobby_core::grant::DirectConnections::postgres(&self.database_url),
            );
            command.output().expect("run gcode index")
        }

        fn hold_file_lock(&self, file_path: &str) -> Client {
            let mut conn = Client::connect(&self.database_url, NoTls).expect("connect lock holder");
            let project_key = project_lock_key(&self.project_id);
            let file_key = file_lock_key(&self.project_id, file_path);
            conn.query_one("SELECT pg_advisory_lock_shared($1)", &[&project_key])
                .expect("hold shared project gate");
            conn.query_one("SELECT pg_advisory_lock($1)", &[&file_key])
                .expect("hold file lock");
            conn
        }

        fn hold_project_lock(&self) -> Client {
            let mut conn = Client::connect(&self.database_url, NoTls).expect("connect lock holder");
            let project_key = project_lock_key(&self.project_id);
            conn.query_one("SELECT pg_advisory_lock($1)", &[&project_key])
                .expect("hold project lock");
            conn
        }
    }

    impl Drop for TestProject {
        fn drop(&mut self) {
            if let Ok(mut conn) = Client::connect(&self.database_url, NoTls) {
                let _ = cleanup_project(&mut conn, &self.project_id);
            }
        }
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn disjoint_cli_file_index_overlaps_and_alias_contends() {
        let project = TestProject::new("disjoint-alias");
        let _held = project.hold_file_lock("src/held.rs");

        let disjoint = project.run_index("json", false, &["src/free.rs"]);
        assert!(
            disjoint.status.success(),
            "disjoint file should index while another file is locked: {}",
            String::from_utf8_lossy(&disjoint.stderr)
        );
        let disjoint_payload: Value =
            serde_json::from_slice(&disjoint.stdout).expect("disjoint output is JSON");
        assert_eq!(
            disjoint_payload["completed_files"],
            serde_json::json!(["src/free.rs"])
        );
        assert_eq!(disjoint_payload["busy_files"], serde_json::json!([]));

        let alias = project.run_index("json", false, &["./src/../src/held.rs"]);
        assert_eq!(alias.status.code(), Some(3));
        let alias_payload: Value =
            serde_json::from_slice(&alias.stdout).expect("alias contention output is JSON");
        assert_eq!(alias_payload["completed_files"], serde_json::json!([]));
        assert_eq!(
            alias_payload["busy_files"],
            serde_json::json!(["src/held.rs"])
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn partial_and_all_busy_cli_contracts_are_machine_readable() {
        let project = TestProject::new("partial-all-busy");
        let _held = project.hold_file_lock("src/held.rs");

        let partial = project.run_index("json", false, &["src/held.rs", "src/free.rs"]);
        assert_eq!(partial.status.code(), Some(0));
        let partial_payload: Value =
            serde_json::from_slice(&partial.stdout).expect("partial output is JSON");
        assert_eq!(
            partial_payload["completed_files"],
            serde_json::json!(["src/free.rs"])
        );
        assert_eq!(
            partial_payload["busy_files"],
            serde_json::json!(["src/held.rs"])
        );

        let _free = project.hold_file_lock("src/free.rs");
        let all_busy_json = project.run_index("json", false, &["src/held.rs", "src/free.rs"]);
        assert_eq!(all_busy_json.status.code(), Some(3));
        let all_busy_payload: Value =
            serde_json::from_slice(&all_busy_json.stdout).expect("all-busy output is JSON");
        assert_eq!(all_busy_payload["completed_files"], serde_json::json!([]));
        assert_eq!(
            all_busy_payload["busy_files"],
            serde_json::json!(["src/free.rs", "src/held.rs"])
        );

        let all_busy_text = project.run_index("text", false, &["src/held.rs", "src/free.rs"]);
        assert_eq!(all_busy_text.status.code(), Some(3));
        let stdout = String::from_utf8_lossy(&all_busy_text.stdout);
        assert!(stdout.contains("completed_files: []"), "stdout={stdout}");
        assert!(
            stdout.contains("busy_files: [\"src/free.rs\",\"src/held.rs\"]"),
            "stdout={stdout}"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn project_and_file_cli_locks_exclude_each_other() {
        let project = TestProject::new("project-file-exclusion");

        let file_holder = project.hold_file_lock("src/held.rs");
        let full = project.run_index("json", true, &[]);
        assert_eq!(full.status.code(), Some(3));
        drop(file_holder);

        let _project_holder = project.hold_project_lock();
        let file = project.run_index("json", false, &["src/free.rs"]);
        assert_eq!(file.status.code(), Some(3));
        let payload: Value =
            serde_json::from_slice(&file.stdout).expect("file contention output is JSON");
        assert_eq!(payload["completed_files"], serde_json::json!([]));
        assert_eq!(payload["busy_files"], serde_json::json!(["src/free.rs"]));
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn file_scope_errors_do_not_use_busy_exit() {
        let project = TestProject::new("path-error");

        let output = project.run_index("json", false, &["../outside.rs"]);
        assert!(!output.status.success());
        assert_ne!(output.status.code(), Some(3));
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("outside") && stderr.contains("project scope"),
            "stderr={stderr}"
        );
    }

    fn isolated_gobby_home(root: &Path) -> PathBuf {
        let home = root.join(".no-daemon-home");
        std::fs::create_dir_all(&home).expect("create isolated Gobby home");
        std::fs::write(home.join("machine_id"), local_machine_uuid().to_string())
            .expect("write isolated machine id");
        home
    }

    fn local_machine_uuid() -> uuid::Uuid {
        uuid::Uuid::parse_str(
            &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
        )
        .expect("local machine id is a uuid")
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
        command
            .env_remove("GOBBY_AGENT_RUN_ID")
            .env_remove("GOBBY_MANAGED_EXECUTION_ID")
            .env("GOBBY_HOME", home)
            .env("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", path);
    }

    fn project_lock_key(project_id: &str) -> i64 {
        advisory_key(b"gcode:index:", project_id, None)
    }

    fn file_lock_key(project_id: &str, file_path: &str) -> i64 {
        advisory_key(b"gcode:index-file:", project_id, Some(file_path))
    }

    fn advisory_key(prefix: &[u8], project_id: &str, file_path: Option<&str>) -> i64 {
        let mut hasher = Sha256::new();
        hasher.update(prefix);
        hasher.update(project_id.as_bytes());
        if let Some(file_path) = file_path {
            hasher.update(b":");
            hasher.update(file_path.as_bytes());
        }
        let digest = hasher.finalize();
        i64::from_be_bytes(
            digest[0..8]
                .try_into()
                .expect("SHA-256 digest has at least 8 bytes"),
        )
    }

    fn cleanup_project(conn: &mut Client, project_id: &str) -> anyhow::Result<()> {
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
            "DELETE FROM code_calls WHERE project_id = $1",
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
}
