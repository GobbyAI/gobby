//! Embedded gcode skill for AI CLI agents.
//!
//! Bundles the SKILL.md content and installs it to every supported
//! project-level AI CLI skill target. Gemini CLI remains installed for
//! compatibility, but it is deprecated.

use std::path::Path;

/// The embedded SKILL.md content.
const SKILL_CONTENT: &str = include_str!("../assets/SKILL.md");

/// Claude Code plugin.json manifest.
const PLUGIN_JSON: &str = r#"{
  "name": "gcode",
  "description": "AST-aware code search, symbol navigation, and dependency graph analysis",
  "version": "0.1.0"
}"#;

/// AI CLI skill target supported by `gcode init`.
#[derive(Debug, Clone, Copy)]
pub struct SkillTarget {
    pub display_name: &'static str,
    kind: InstallKind,
}

#[derive(Debug, Clone, Copy)]
enum InstallKind {
    ClaudePlugin,
    SkillDir { cli_dir: &'static str },
}

const SKILL_TARGETS: &[SkillTarget] = &[
    SkillTarget {
        display_name: "Claude Code",
        kind: InstallKind::ClaudePlugin,
    },
    SkillTarget {
        display_name: "Codex",
        kind: InstallKind::SkillDir { cli_dir: ".codex" },
    },
    SkillTarget {
        display_name: "Droid",
        kind: InstallKind::SkillDir {
            cli_dir: ".factory",
        },
    },
    SkillTarget {
        display_name: "Grok",
        kind: InstallKind::SkillDir { cli_dir: ".grok" },
    },
    SkillTarget {
        display_name: "Qwen",
        kind: InstallKind::SkillDir { cli_dir: ".qwen" },
    },
    // Gemini CLI is deprecated; keep writing the skill for older setups.
    SkillTarget {
        display_name: "Gemini CLI (deprecated)",
        kind: InstallKind::SkillDir { cli_dir: ".gemini" },
    },
    SkillTarget {
        display_name: "Antigravity CLI",
        kind: InstallKind::SkillDir { cli_dir: ".agents" },
    },
];

/// All supported AI CLI skill targets.
pub fn supported_targets() -> &'static [SkillTarget] {
    SKILL_TARGETS
}

/// Install the gcode skill for a supported CLI target.
/// Returns the path where the skill was installed.
pub fn install_skill(project_root: &Path, target: &SkillTarget) -> std::io::Result<String> {
    match target.kind {
        InstallKind::ClaudePlugin => install_claude_plugin(project_root),
        InstallKind::SkillDir { cli_dir } => install_skill_dir(project_root, cli_dir),
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

#[cfg(test)]
mod tests {
    use super::*;

    fn target_path(project_root: &Path, target: &SkillTarget) -> std::path::PathBuf {
        match target.kind {
            InstallKind::ClaudePlugin => project_root.join("skills/gcode/SKILL.md"),
            InstallKind::SkillDir { cli_dir } => {
                project_root.join(cli_dir).join("skills/gcode/SKILL.md")
            }
        }
    }

    fn expected_reported_path(target: &SkillTarget) -> String {
        match target.kind {
            InstallKind::ClaudePlugin => "skills/gcode/SKILL.md".to_string(),
            InstallKind::SkillDir { cli_dir } => format!("{cli_dir}/skills/gcode/SKILL.md"),
        }
    }

    #[test]
    fn plugin_json_is_valid() {
        let manifest: serde_json::Value =
            serde_json::from_str(PLUGIN_JSON).expect("plugin json parses");

        assert_eq!(manifest["name"], "gcode");
        assert_eq!(manifest["version"], "0.1.0");
        assert!(
            manifest["description"]
                .as_str()
                .is_some_and(|s| !s.is_empty())
        );
    }

    #[test]
    fn supported_targets_are_stable_and_include_deprecated_gemini() {
        let names: Vec<_> = supported_targets()
            .iter()
            .map(|target| target.display_name)
            .collect();

        assert_eq!(
            names,
            vec![
                "Claude Code",
                "Codex",
                "Droid",
                "Grok",
                "Qwen",
                "Gemini CLI (deprecated)",
                "Antigravity CLI",
            ]
        );
    }

    #[test]
    fn installs_skill_to_all_supported_target_paths() {
        let tmp = tempfile::tempdir().expect("tempdir");

        for target in supported_targets() {
            let installed_path = install_skill(tmp.path(), target).expect("install skill");
            let skill_path = target_path(tmp.path(), target);

            assert_eq!(
                std::fs::read_to_string(&skill_path).expect("read installed skill"),
                SKILL_CONTENT
            );
            assert_eq!(installed_path, expected_reported_path(target));
        }
    }

    #[test]
    fn claude_plugin_manifest_is_written() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let target = supported_targets()
            .iter()
            .find(|target| target.display_name == "Claude Code")
            .expect("claude target");

        let reported_path = install_skill(tmp.path(), target).expect("install claude skill");
        let manifest_path = tmp.path().join(".claude-plugin/plugin.json");
        let manifest: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(manifest_path).expect("read plugin manifest"),
        )
        .expect("parse plugin manifest");

        assert_eq!(reported_path, "skills/gcode/SKILL.md");
        assert_eq!(manifest["name"], "gcode");
        assert_eq!(
            manifest["description"],
            "AST-aware code search, symbol navigation, and dependency graph analysis"
        );
        assert_eq!(manifest["version"], "0.1.0");
    }

    #[test]
    fn gemini_is_deprecated_but_still_installed() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let target = supported_targets()
            .iter()
            .find(|target| target.display_name == "Gemini CLI (deprecated)")
            .expect("gemini target");

        let reported_path = install_skill(tmp.path(), target).expect("install gemini skill");

        assert_eq!(reported_path, ".gemini/skills/gcode/SKILL.md");
        assert_eq!(
            std::fs::read_to_string(tmp.path().join(&reported_path)).expect("read gemini skill"),
            SKILL_CONTENT
        );
    }

    #[test]
    fn installing_skills_does_not_delete_existing_cli_files() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let sentinels = [
            ".codex/config.toml",
            ".factory/settings.json",
            ".grok/notes.md",
            ".qwen/state.json",
            ".gemini/settings.json",
            ".agents/memory.md",
            ".claude-plugin/existing.json",
            "skills/custom/SKILL.md",
        ];

        for path in sentinels {
            let path = tmp.path().join(path);
            std::fs::create_dir_all(path.parent().expect("sentinel parent"))
                .expect("create sentinel parent");
            std::fs::write(&path, "keep").expect("write sentinel");
        }

        for target in supported_targets() {
            install_skill(tmp.path(), target).expect("install skill");
        }

        for path in sentinels {
            assert_eq!(
                std::fs::read_to_string(tmp.path().join(path)).expect("read sentinel"),
                "keep"
            );
        }
    }
}
