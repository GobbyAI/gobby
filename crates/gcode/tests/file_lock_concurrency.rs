#[cfg(test)]
mod serial_db {
    use postgres::{Client, NoTls};
    use serde_json::Value;
    use sha2::{Digest, Sha256};
    use std::path::{Path, PathBuf};
    use std::process::{Command, Output};
    use std::time::{Duration, Instant};

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
            test_env::seed_test_principal_binding(&mut conn, &project_id)
                .expect("seed managed principal binding");

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

    struct LinkedReadFixture {
        parent: TestProject,
        checkout: tempfile::TempDir,
        overlay_id: String,
    }

    impl LinkedReadFixture {
        fn new(label: &str) -> Self {
            let parent = TestProject::new(label);
            std::fs::write(
                parent.root.path().join("src/free.rs"),
                "pub fn free() {}\n// held|free\n",
            )
            .expect("write indexed literal pipe control");
            let indexed = parent.run_index("json", true, &[]);
            assert!(indexed.status.success(), "parent index: {indexed:?}");
            for args in [
                vec!["add", "src", ".gobby/project.json"],
                vec![
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
            ] {
                let output = Command::new("git")
                    .current_dir(parent.root.path())
                    .args(args)
                    .output()
                    .expect("prepare linked checkout");
                assert!(output.status.success(), "git fixture: {output:?}");
            }
            let checkout = tempfile::tempdir().expect("linked checkout directory");
            let output = Command::new("git")
                .current_dir(parent.root.path())
                .args(["worktree", "add", "--quiet", "--detach"])
                .arg(checkout.path())
                .output()
                .expect("git worktree add");
            assert!(output.status.success(), "linked checkout: {output:?}");
            assert!(checkout.path().join(".git").is_file());
            std::fs::write(
                checkout.path().join(".gobby/isolation.json"),
                serde_json::json!({
                    "parent_project_id": parent.project_id,
                    "parent_project_path": parent.root.path(),
                })
                .to_string(),
            )
            .expect("write overlay identity");
            let overlay_id = gobby_core::project::code_index_id_for_root(checkout.path());
            let mut conn = Client::connect(&parent.database_url, NoTls).expect("overlay setup");
            test_env::seed_test_checkout(&mut conn, &overlay_id, checkout.path())
                .expect("register linked overlay checkout");
            conn.execute(
                "UPDATE gobby_agent_auth.principal_bindings SET code_overlay_project_id = $1
                 WHERE project_id = $2",
                &[
                    &uuid::Uuid::parse_str(&overlay_id).expect("overlay uuid"),
                    &uuid::Uuid::parse_str(&parent.project_id).expect("parent uuid"),
                ],
            )
            .expect("freeze the fixture principal's admitted overlay");
            let fixture = Self {
                parent,
                checkout,
                overlay_id,
            };
            let indexed = fixture
                .command(&["index", "--quiet", "--skip-if-locked"])
                .output()
                .expect("index overlay");
            assert!(indexed.status.success(), "overlay index: {indexed:?}");
            // Force project freshness to try the held refresh lock on each read.
            std::fs::write(
                fixture.checkout.path().join("src/free.rs"),
                "pub fn free() {}\n// held|free\npub fn refresh_pending() {}\n",
            )
            .expect("change linked checkout");
            fixture
        }

        fn command(&self, args: &[&str]) -> Command {
            self.command_in(self.checkout.path(), args)
        }

        /// The overlay principal's command, run from `cwd`. The parent checkout lies
        /// outside that principal's write scope, so reads of it are read-only.
        fn command_in(&self, cwd: &Path, args: &[&str]) -> Command {
            let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
            command
                .current_dir(cwd)
                .args(["--format", "json"])
                .args(args);
            let machine = std::fs::read_to_string(self.parent.home.join("machine_id"))
                .expect("fixture machine");
            let mut grant = gobby_core::grant::managed_direct_grant(
                &self.parent.project_id,
                machine.trim(),
                &gobby_core::grant::DirectConnections::postgres(&self.parent.database_url),
            );
            grant.principal.code_overlay_project_id = Some(self.overlay_id.clone());
            let path = gobby_core::grant::write_managed_bootstrap(
                &self.parent.home.join("grants"),
                &grant.with_checksum(),
            )
            .expect("overlay managed grant");
            command
                .env("GOBBY_HOME", &self.parent.home)
                .env("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", path)
                .env_remove("GOBBY_AGENT_RUN_ID")
                .env_remove("GOBBY_MANAGED_EXECUTION_ID");
            command
        }

        fn write_unindexed_files(&self, count: usize) {
            write_unindexed_files_in(self.checkout.path(), count);
        }

        fn read_while_refresh_locked(&self, args: &[&str]) -> Output {
            let mut holder =
                Client::connect(&self.parent.database_url, NoTls).expect("refresh lock connection");
            holder
                .batch_execute("SET application_name = 'gobby-agent-idle-refresh-fixture'")
                .expect("name idle holder");
            holder
                .query_one(
                    "SELECT pg_advisory_lock($1)",
                    &[&project_lock_key(&self.overlay_id)],
                )
                .expect("hold overlay refresh lock");
            let pid: i32 = holder
                .query_one("SELECT pg_backend_pid()", &[])
                .expect("holder pid")
                .get(0);
            let mut observer = Client::connect(&self.parent.database_url, NoTls)
                .expect("observe idle holder from another connection");
            let idle = observer
                .query_one(
                    "SELECT state, xact_start IS NULL, application_name FROM pg_stat_activity
                 WHERE pid = $1",
                    &[&pid],
                )
                .expect("idle-holder observation");
            assert_eq!(idle.get::<_, String>(0), "idle");
            assert!(
                idle.get::<_, bool>(1),
                "session lock holder has no open transaction"
            );
            assert_eq!(idle.get::<_, String>(2), "gobby-agent-idle-refresh-fixture");
            let started = Instant::now();
            let mut child = self
                .command(args)
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .spawn()
                .expect("spawn read command");
            let completed = loop {
                if child.try_wait().expect("poll read command").is_some() {
                    break true;
                }
                if started.elapsed() >= Duration::from_secs(20) {
                    child.kill().expect("reap stalled read command");
                    break false;
                }
                std::thread::sleep(Duration::from_millis(20));
            };
            let output = child.wait_with_output().expect("collect read command");
            eprintln!(
                "read={args:?} holder_pid={pid} state=idle transaction=none elapsed={:?} status={} stderr={}",
                started.elapsed(),
                output.status,
                String::from_utf8_lossy(&output.stderr),
            );
            let idle = observer
                .query_one("SELECT state FROM pg_stat_activity WHERE pid = $1", &[&pid])
                .expect("holder remains alive until after read");
            assert_eq!(idle.get::<_, String>(0), "idle");
            drop(holder);
            assert!(
                completed,
                "read must finish while refresh remains locked: {args:?}: {output:?}"
            );
            assert!(
                String::from_utf8_lossy(&output.stderr).contains("refresh already running"),
                "a read that finds the refresh lock held must report it busy: {output:?}"
            );
            if output.status.code() == Some(2) {
                let stderr = String::from_utf8_lossy(&output.stderr);
                let error = stderr
                    .lines()
                    .filter_map(|line| serde_json::from_str::<Value>(line).ok())
                    .find(|value| value["error"].is_string())
                    .expect("typed error JSON after any freshness warning");
                assert!(error["message"].is_string(), "typed error message: {error}");
            }
            output
        }

        /// Runs a read with no refresh holder, killing it once `bound` has passed.
        fn read_within(&self, mut command: Command, bound: Duration) -> (Output, bool) {
            let started = Instant::now();
            let mut child = command
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .spawn()
                .expect("spawn read command");
            let completed = loop {
                if child.try_wait().expect("poll read command").is_some() {
                    break true;
                }
                if started.elapsed() >= bound {
                    child.kill().expect("reap stalled read command");
                    break false;
                }
                std::thread::sleep(Duration::from_millis(20));
            };
            let output = child.wait_with_output().expect("collect read command");
            (output, completed)
        }
    }

    impl Drop for LinkedReadFixture {
        fn drop(&mut self) {
            let mut conn = Client::connect(&self.parent.database_url, NoTls)
                .expect("overlay cleanup connection");
            conn.execute(
                "DELETE FROM code_overlay_pins WHERE overlay_project_id = $1",
                &[&uuid::Uuid::parse_str(&self.overlay_id).expect("overlay uuid")],
            )
            .expect("clear overlay pins");
            cleanup_project(&mut conn, &self.overlay_id).expect("clear linked overlay");
        }
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn linked_worktree_search_symbol_returns_while_refresh_lock_is_held() {
        let fixture = LinkedReadFixture::new("search-symbol-refresh-lock");
        let output = fixture.read_while_refresh_locked(&["search-symbol", "free"]);
        assert!(
            output.status.success() || output.status.code() == Some(2),
            "{output:?}"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn linked_worktree_literal_grep_returns_while_refresh_lock_is_held() {
        let fixture = LinkedReadFixture::new("literal-grep-refresh-lock");
        let output = fixture.read_while_refresh_locked(&["grep", "-F", "pub fn", "-m", "50"]);
        assert!(
            output.status.success() || output.status.code() == Some(2),
            "{output:?}"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn linked_worktree_search_symbol_returns_within_bound_without_refresh_holder() {
        let fixture = LinkedReadFixture::new("search-symbol-no-holder");
        fixture.write_unindexed_files(400);
        let (output, completed) = fixture.read_within(
            fixture.command(&["search-symbol", "free"]),
            Duration::from_secs(20),
        );
        assert!(
            completed,
            "read must return within 20s with no refresh holder: {output:?}"
        );
        assert!(
            output.status.success() || output.status.code() == Some(2),
            "{output:?}"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn linked_worktree_grep_returns_within_bound_with_many_stale_files() {
        let fixture = LinkedReadFixture::new("grep-stale-budget");
        fixture.write_unindexed_files(400);
        let (output, completed) = fixture.read_within(
            fixture.command(&["grep", "-F", "pub fn", "-m", "50"]),
            Duration::from_secs(10),
        );
        assert!(
            completed,
            "grep must return within 10s over 400 stale files: {output:?}"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("behind the worktree"),
            "a read past the foreground budget must report its backlog: {stderr}"
        );
        assert!(output.status.success(), "{output:?}");
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn linked_worktree_status_and_graph_reads_return_within_bound_with_many_stale_files() {
        let fixture = LinkedReadFixture::new("status-graph-stale-budget");
        fixture.write_unindexed_files(400);
        for args in [
            &["status"][..],
            &["graph", "overview"][..],
            &["graph", "report"][..],
        ] {
            let (output, completed) =
                fixture.read_within(fixture.command(args), Duration::from_secs(20));
            assert!(
                completed,
                "{args:?} must return within 20s over 400 stale files: {output:?}"
            );
            let stderr = String::from_utf8_lossy(&output.stderr);
            assert!(
                stderr.contains("behind the worktree"),
                "{args:?} past the foreground budget must report its backlog: {stderr}"
            );
        }
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn linked_parent_grep_reads_read_only_and_leaves_out_stale_files_past_budget() {
        let fixture = LinkedReadFixture::new("parent-grep-stale-budget");
        write_unindexed_files_in(fixture.parent.root.path(), 400);
        let (output, completed) = fixture.read_within(
            fixture.command_in(
                fixture.parent.root.path(),
                &["grep", "-F", "pub fn", "-m", "50"],
            ),
            Duration::from_secs(10),
        );
        assert!(
            completed,
            "a read-only grep over 400 stale files must return within 10s: {output:?}"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("cannot refresh"),
            "a read-only checkout must report its stale paths: {stderr}"
        );
        assert!(
            stderr.contains("left out of grep"),
            "grep past the foreground budget must name the stale paths it left out: {stderr}"
        );
        assert!(output.status.success(), "{output:?}");
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn linked_worktree_cli_grep_alternation_and_literal_pipes_agree_with_regex_syntax() {
        let fixture = LinkedReadFixture::new("grep-alternation-refresh-lock");
        for (pattern, fixed, expected) in [
            (
                "held|free",
                false,
                vec!["// held|free", "pub fn free() {}", "pub fn held() {}"],
            ),
            (r"held\|free", false, vec!["// held|free"]),
            ("held|free", true, vec!["// held|free"]),
        ] {
            let mut args = vec!["grep", "-n", pattern, "src", "-m", "50"];
            if fixed {
                args.push("-F");
            }
            let output = fixture.read_while_refresh_locked(&args);
            assert!(
                output.status.success(),
                "grep {pattern}, fixed={fixed}: {output:?}"
            );
            let payload: Value = serde_json::from_slice(&output.stdout).expect("grep JSON");
            let mut lines: Vec<_> = payload["matches"]
                .as_array()
                .expect("grep matches")
                .iter()
                .map(|row| row["text"].as_str().expect("match text"))
                .collect();
            lines.sort_unstable();
            assert_eq!(lines, expected, "CLI pattern={pattern}, fixed={fixed}");
        }
    }

    /// Adds files `root` has not indexed, so every read sees stale paths.
    fn write_unindexed_files_in(root: &Path, count: usize) {
        for index in 0..count {
            std::fs::write(
                root.join(format!("src/unindexed_{index}.rs")),
                format!("pub fn unindexed_{index}() -> usize {{ {index} }}\n"),
            )
            .expect("write unindexed file");
        }
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
