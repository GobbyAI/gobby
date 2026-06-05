#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{SystemTime, UNIX_EPOCH};

use tempfile::TempDir;

pub const PROJECT_ID: &str = "project-123";
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
        self.hub.join("topics").join(topic)
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
        command.env("GWIKI_DATABASE_URL", database_url);
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
        command
            .env("GOBBY_WIKI_HUB", &self.hub)
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
    for key in [
        "GWIKI_DATABASE_URL",
        "GWIKI_POSTGRES_TEST_DATABASE_URL",
        "GOBBY_POSTGRES_DSN",
        "GCODE_DATABASE_URL",
        "GCODE_POSTGRES_TEST_DATABASE_URL",
        "GOBBY_FALKORDB_HOST",
        "GOBBY_FALKORDB_PORT",
        "GOBBY_FALKORDB_PASSWORD",
        "GOBBY_QDRANT_URL",
        "GOBBY_QDRANT_API_KEY",
        "GOBBY_EMBEDDING_URL",
        "GOBBY_EMBEDDING_MODEL",
        "GOBBY_EMBEDDING_API_KEY",
        "GOBBY_EMBEDDING_QUERY_PREFIX",
        "GOBBY_EMBEDDING_TIMEOUT_SECONDS",
        "GOBBY_HOME",
    ] {
        command.env_remove(key);
    }
    command
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
