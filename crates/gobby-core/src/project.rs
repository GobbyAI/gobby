//! Project-root discovery and project-id reading.
//!
//! Kept in lockstep with `gcode/src/project.rs` until PR 4 (R2-08) migrates
//! gcode to import from here.

use anyhow::Context;
use std::path::{Path, PathBuf};

/// Walk up from `start` looking for a `.gobby` directory containing either
/// `project.json` or `gcode.json`. Returns the project root (the directory
/// containing `.gobby`) or `None` if no project is found before hitting the
/// filesystem root.
pub fn find_project_root(start: &Path) -> Option<PathBuf> {
    let mut dir = start;
    loop {
        let gobby_dir = dir.join(".gobby");
        if gobby_dir.join("project.json").exists() || gobby_dir.join("gcode.json").exists() {
            return Some(dir.to_path_buf());
        }
        match dir.parent() {
            Some(parent) => dir = parent,
            None => return None,
        }
    }
}

/// Read the project id from `<project_root>/.gobby/project.json`. Accepts
/// either `id` or `project_id` as the key (legacy fallback).
pub fn read_project_id(project_root: &Path) -> anyhow::Result<String> {
    let path = project_root.join(".gobby").join("project.json");
    let contents = std::fs::read_to_string(&path)
        .with_context(|| format!("failed to read {}", path.display()))?;
    let json: serde_json::Value = serde_json::from_str(&contents)?;
    json.get("id")
        .or_else(|| json.get("project_id"))
        .and_then(|v| v.as_str())
        .map(String::from)
        .context("'id' field not found in .gobby/project.json")
}
