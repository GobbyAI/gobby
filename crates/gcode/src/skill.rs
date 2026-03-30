//! Embedded gcode skill for AI CLI agents.
//!
//! Bundles the SKILL.md content and installs it to project-level
//! skill directories for detected AI CLIs (Claude Code, Gemini, Codex).

use std::path::Path;

/// The embedded SKILL.md content.
const SKILL_CONTENT: &str = include_str!("../assets/SKILL.md");

/// Claude Code plugin.json manifest.
const PLUGIN_JSON: &str = r#"{
  "name": "gcode",
  "description": "AST-aware code search, symbol navigation, and dependency graph analysis",
  "version": "0.1.0"
}"#;

/// AI CLI that supports skill installation.
#[derive(Debug)]
pub struct DetectedCli {
    pub name: &'static str,
    pub dir: &'static str,
}

/// Detect AI CLI directories at the project root.
pub fn detect_clis(project_root: &Path) -> Vec<DetectedCli> {
    let candidates = [
        DetectedCli {
            name: "Claude Code",
            dir: ".claude",
        },
        DetectedCli {
            name: "Gemini",
            dir: ".gemini",
        },
        DetectedCli {
            name: "Codex",
            dir: ".codex",
        },
    ];

    candidates
        .into_iter()
        .filter(|cli| project_root.join(cli.dir).is_dir())
        .collect()
}

/// Install the gcode skill for a detected CLI.
/// Returns the path where the skill was installed.
pub fn install_skill(project_root: &Path, cli: &DetectedCli) -> std::io::Result<String> {
    match cli.dir {
        ".claude" => install_claude_plugin(project_root),
        ".gemini" | ".codex" => install_skill_dir(project_root, cli.dir),
        _ => Ok(String::new()),
    }
}

/// Install as a Claude Code plugin with plugin.json + skills/gcode/SKILL.md
fn install_claude_plugin(project_root: &Path) -> std::io::Result<String> {
    let plugin_dir = project_root.join(".claude-plugin");
    std::fs::create_dir_all(&plugin_dir)?;
    std::fs::write(plugin_dir.join("plugin.json"), PLUGIN_JSON)?;

    let skill_dir = project_root.join("skills").join("gcode");
    std::fs::create_dir_all(&skill_dir)?;
    std::fs::write(skill_dir.join("SKILL.md"), SKILL_CONTENT)?;

    Ok("skills/gcode/SKILL.md".to_string())
}

/// Install as a SKILL.md in the CLI's skills directory.
fn install_skill_dir(project_root: &Path, cli_dir: &str) -> std::io::Result<String> {
    let skill_dir = project_root.join(cli_dir).join("skills").join("gcode");
    std::fs::create_dir_all(&skill_dir)?;
    std::fs::write(skill_dir.join("SKILL.md"), SKILL_CONTENT)?;

    Ok(format!("{}/skills/gcode/SKILL.md", cli_dir))
}
