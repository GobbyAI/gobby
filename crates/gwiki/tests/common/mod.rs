#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

pub const PROJECT_ID: &str = "project-123";
pub const GCODE_JSON: &str = r#"{
  "id": "project-123",
  "name": "gcode-fixture"
}
"#;

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
