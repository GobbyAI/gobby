#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{SystemTime, UNIX_EPOCH};

use tempfile::TempDir;

pub const PROJECT_ID: &str = "project-123";
const MANAGED_AGENT_ENV_KEYS: &[&str] = &[
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_MANAGED_EXECUTION_ID",
    gobby_core::grant::MANAGED_BOOTSTRAP_ENV,
    "GOBBY_SESSION_ID",
    "GOBBY_PARENT_SESSION_ID",
    gobby_core::local_token::AGENT_API_TOKEN_ENV,
    "GOBBY_DAEMON_URL",
    "GOBBY_PORT",
    "GOBBY_DAEMON_PORT",
];
pub const GCODE_JSON: &str = r#"{
  "id": "project-123",
  "name": "gcode-fixture"
}
"#;

const GWIKI_SCOPE_TABLES: &[&str] = &[
    "gwiki_ingestions",
    "gwiki_links",
    "gwiki_chunks",
    "gwiki_sources",
    "gwiki_documents",
];

fn validated_gwiki_scope_table_name(table: &str) -> Option<&'static str> {
    match table {
        "gwiki_ingestions" => Some("gwiki_ingestions"),
        "gwiki_links" => Some("gwiki_links"),
        "gwiki_chunks" => Some("gwiki_chunks"),
        "gwiki_sources" => Some("gwiki_sources"),
        "gwiki_documents" => Some("gwiki_documents"),
        _ => None,
    }
}

pub struct GwikiFixture {
    _tempdir: TempDir,
    root: PathBuf,
    hub: PathBuf,
    home: PathBuf,
    project: PathBuf,
}

pub struct InitializedTopic {
    pub name: String,
    pub vault: PathBuf,
}

impl GwikiFixture {
    pub fn new() -> Self {
        let tempdir = tempfile::tempdir().expect("tempdir");
        let root = tempdir.path().to_path_buf();
        let hub = root.join("hub");
        let home = root.join("home");
        let project = root.join("project");
        fs::create_dir_all(&home).expect("create isolated home");
        fs::create_dir_all(&project).expect("create isolated project");

        Self {
            _tempdir: tempdir,
            root,
            hub,
            home,
            project,
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn hub(&self) -> &Path {
        &self.hub
    }

    pub fn home(&self) -> &Path {
        &self.home
    }

    pub fn project(&self) -> &Path {
        &self.project
    }

    pub fn topic_vault(&self, topic: &str) -> PathBuf {
        self.hub.join(topic)
    }

    pub fn command(&self) -> Command {
        self.command_in(self.root())
    }

    pub fn command_in(&self, cwd: &Path) -> Command {
        let mut command = gwiki_command();
        self.apply_isolated_env(&mut command).current_dir(cwd);
        command
    }

    pub fn command_in_project(&self) -> Command {
        self.command_in(self.project())
    }

    pub fn command_with_database_url_in(&self, cwd: &Path, database_url: &str) -> Command {
        let mut command = self.command_in(cwd);
        attach_managed_grant(&mut command, self.home(), PROJECT_ID, database_url);
        command
    }

    pub fn output(&self, args: &[&str]) -> Output {
        self.output_in(self.root(), args)
    }

    pub fn output_in(&self, cwd: &Path, args: &[&str]) -> Output {
        self.command_in(cwd)
            .args(args)
            .output()
            .expect("gwiki binary runs")
    }

    pub fn output_in_project(&self, args: &[&str]) -> Output {
        self.output_in(self.project(), args)
    }

    pub fn output_with_database_url_in(
        &self,
        cwd: &Path,
        database_url: &str,
        args: &[&str],
    ) -> Output {
        self.command_with_database_url_in(cwd, database_url)
            .args(args)
            .output()
            .expect("gwiki binary runs")
    }

    pub fn init_topic(&self, label: &str) -> InitializedTopic {
        let name = unique_topic(label);
        let output = self.output(&["init", "--topic", &name]);
        assert_success(&output, "topic init");
        InitializedTopic {
            vault: self.topic_vault(&name),
            name,
        }
    }

    fn apply_isolated_env<'a>(&self, command: &'a mut Command) -> &'a mut Command {
        scrub_managed_agent_env(command);
        command
            .env("GOBBY_WIKI_HUB", &self.hub)
            .env("GOBBY_HOME", &self.home)
            .env("HOME", &self.home)
            .env("XDG_CONFIG_HOME", self.root.join("xdg-config"))
            .env("XDG_DATA_HOME", self.root.join("xdg-data"))
            .env("XDG_CACHE_HOME", self.root.join("xdg-cache"))
            .env("XDG_STATE_HOME", self.root.join("xdg-state"))
    }
}

pub fn write_gcode_json(project: &Path) -> PathBuf {
    write_gobby_fixture(project, "gcode.json", GCODE_JSON)
}

fn write_gobby_fixture(project: &Path, file_name: &str, contents: &str) -> PathBuf {
    let gobby_dir = project.join(".gobby");
    fs::create_dir_all(&gobby_dir).expect("create .gobby");
    let path = gobby_dir.join(file_name);
    fs::write(&path, contents).expect("write .gobby fixture");
    path
}

pub fn assert_gcode_json_unchanged(path: &Path) {
    assert_eq!(
        fs::read_to_string(path).expect("read gcode json"),
        GCODE_JSON
    );
}

pub fn gwiki_command() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_gwiki"));
    strip_service_env(&mut command);
    command
}

pub fn assert_success(output: &Output, label: &str) {
    assert!(
        output.status.success(),
        "{label} failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

pub fn assert_daemon_required(output: &Output, label: &str) {
    assert!(
        !output.status.success(),
        "{label} succeeded without a daemon\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    let payload = parse_json_error_payload(output);
    assert_eq!(
        payload.get("code").and_then(|value| value.as_str()),
        Some("daemon_required"),
        "{label} stderr:\n{stderr}"
    );
}

pub fn parse_json_error_payload(output: &Output) -> serde_json::Value {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    for stream in [stderr.as_ref(), stdout.as_ref()] {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(stream.trim())
            && (value.get("error").is_some() || value.get("code").is_some())
        {
            return value;
        }
        for line in stream.lines().rev() {
            let trimmed = line.trim();
            if let Ok(value) = serde_json::from_str::<serde_json::Value>(trimmed)
                && (value.get("error").is_some() || value.get("code").is_some())
            {
                return value;
            }
        }
    }
    panic!("expected JSON error payload\nstdout:\n{stdout}\nstderr:\n{stderr}");
}

pub fn json_stdout(output: &Output) -> serde_json::Value {
    serde_json::from_slice(&output.stdout).expect("stdout is JSON")
}

pub fn json_stderr(output: &Output) -> serde_json::Value {
    serde_json::from_slice(&output.stderr).expect("stderr is JSON")
}

pub fn unique_topic(label: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time after epoch")
        .as_nanos();
    format!(
        "{label}-{}-{nanos}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    )
}

pub fn postgres_test_database_url() -> Option<String> {
    std::env::var("GWIKI_POSTGRES_TEST_DATABASE_URL")
        .ok()
        .or_else(|| std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL").ok())
        .filter(|value| !value.trim().is_empty())
}

pub struct GwikiScopeCleanup {
    database_url: String,
    scope_kind: &'static str,
    scope_id: String,
}

impl GwikiScopeCleanup {
    pub fn new(database_url: String, scope_kind: &'static str, scope_id: String) -> Self {
        Self {
            database_url,
            scope_kind,
            scope_id,
        }
    }
}

impl Drop for GwikiScopeCleanup {
    fn drop(&mut self) {
        cleanup_gwiki_scope(&self.database_url, self.scope_kind, &self.scope_id);
    }
}

pub fn strip_service_env(command: &mut Command) -> &mut Command {
    scrub_managed_agent_env(command);
    for key in [
        "GWIKI_TEST_DATABASE_URL",
        "GWIKI_POSTGRES_TEST_DATABASE_URL",
        "GOBBY_TEST_POSTGRES_DSN",
        "GCODE_TEST_DATABASE_URL",
        "GCODE_POSTGRES_TEST_DATABASE_URL",
        "GOBBY_HOME",
    ] {
        command.env_remove(key);
    }
    command
}

fn scrub_managed_agent_env(command: &mut Command) {
    for key in MANAGED_AGENT_ENV_KEYS {
        command.env_remove(key);
    }
}

pub fn attach_managed_grant(
    command: &mut Command,
    home: &Path,
    project_id: &str,
    database_url: &str,
) {
    fs::create_dir_all(home).expect("create grant home");
    let machine_path = home.join("machine_id");
    if !machine_path.exists() {
        fs::write(&machine_path, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").expect("machine id");
    }
    let machine = fs::read_to_string(&machine_path).expect("read machine id");
    let grant = gobby_core::grant::managed_direct_grant(
        project_id,
        machine.trim(),
        &gobby_core::grant::DirectConnections::postgres(database_url),
    );
    let path = gobby_core::grant::write_managed_bootstrap(&home.join("grants"), &grant)
        .expect("write managed grant");
    command
        .env("GOBBY_HOME", home)
        .env("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", path);
}

fn cleanup_gwiki_scope(database_url: &str, scope_kind: &str, scope_id: &str) {
    let Ok(mut client) = gobby_core::postgres::connect_readwrite(database_url) else {
        return;
    };
    for table in GWIKI_SCOPE_TABLES {
        let Some(table) = validated_gwiki_scope_table_name(table) else {
            continue;
        };
        let sql = format!("DELETE FROM {table} WHERE scope_kind = $1 AND scope_id = $2");
        let _ = client.execute(&sql, &[&scope_kind, &scope_id]);
    }
}
