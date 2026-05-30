use std::fs;
use std::path::{Path, PathBuf};

pub const PROJECT_ID: &str = "project-123";
pub const PROJECT_JSON: &str = r#"{
  "id": "project-123",
  "name": "demo"
}
"#;
pub const GCODE_JSON: &str = r#"{
  "id": "project-123",
  "name": "gcode-fixture"
}
"#;

pub fn write_project_json(project: &Path) -> PathBuf {
    let gobby_dir = project.join(".gobby");
    fs::create_dir_all(&gobby_dir).expect("create .gobby");
    let path = gobby_dir.join("project.json");
    fs::write(&path, PROJECT_JSON).expect("write project json");
    path
}

pub fn write_gcode_json(project: &Path) -> PathBuf {
    let gobby_dir = project.join(".gobby");
    fs::create_dir_all(&gobby_dir).expect("create .gobby");
    let path = gobby_dir.join("gcode.json");
    fs::write(&path, GCODE_JSON).expect("write gcode json");
    path
}

pub fn assert_project_json_unchanged(path: &Path) {
    assert_eq!(
        fs::read_to_string(path).expect("read project json"),
        PROJECT_JSON
    );
}
