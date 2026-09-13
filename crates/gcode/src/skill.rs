//! Embedded Gobby router for AI CLI agents.
//!
//! Bundles the SKILL.md content and installs it to every supported
//! project-level AI CLI skill target.

use sha2::{Digest, Sha256};
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

/// Install as a Claude Code plugin with plugin.json + skills/gobby/SKILL.md
fn install_claude_plugin(project_root: &Path) -> std::io::Result<String> {
    let plugin_dir = project_root.join(".claude-plugin");
    let manifest = plugin_dir.join("plugin.json");
    match std::fs::read(&manifest) {
        Ok(content) if content == PLUGIN_JSON.as_bytes() => {}
        Ok(_) => {
            return Err(std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                format!("Preserved custom plugin manifest at {}", manifest.display()),
            ));
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }

    let skills_dir = project_root.join("skills");
    let skill_dir = skills_dir.join("gobby");
    std::fs::create_dir_all(&skill_dir)?;
    write_router(&skill_dir.join("SKILL.md"))?;
    std::fs::create_dir_all(&plugin_dir)?;
    std::fs::write(manifest, PLUGIN_JSON)?;
    retire_previous_carrier(&skills_dir)?;

    Ok("skills/gobby/SKILL.md".to_string())
}

/// Install as a SKILL.md in the CLI's skills directory.
fn install_skill_dir(project_root: &Path, cli_dir: &str) -> std::io::Result<String> {
    let skills_dir = project_root.join(cli_dir).join("skills");
    let skill_dir = skills_dir.join("gobby");
    std::fs::create_dir_all(&skill_dir)?;
    write_router(&skill_dir.join("SKILL.md"))?;
    retire_previous_carrier(&skills_dir)?;

    Ok(format!("{}/skills/gobby/SKILL.md", cli_dir))
}

/// Keep an existing current carrier (including generated catalog metadata).
/// Refuse to overwrite custom or unrecognized older instruction bodies.
fn write_router(path: &Path) -> std::io::Result<()> {
    match std::fs::read(path) {
        Ok(content) if content.starts_with(SKILL_CONTENT.as_bytes()) => Ok(()),
        Ok(_) => Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            format!(
                "Preserved existing router at {}. Reconcile this custom or outdated carrier with the bundled gobby router before installing.",
                path.display()
            ),
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            std::fs::write(path, SKILL_CONTENT)
        }
        Err(error) => Err(error),
    }
}

/// Retire only the exact Gobby-owned predecessor; custom instructions stay intact.
fn retire_previous_carrier(skills_dir: &Path) -> std::io::Result<()> {
    let legacy_dir = skills_dir.join("gcode");
    let legacy_file = legacy_dir.join("SKILL.md");
    let content = match std::fs::read(&legacy_file) {
        Ok(content) => content,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let digest: String = Sha256::digest(&content)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    if digest != "fa83fa3f3203912c99e72ec00fbca670f653edc5da5fb16c8eb777bf9d854ee0" {
        eprintln!(
            "Preserved custom skill at {}. Replace retired code-index instructions with gobby:references/code-index/overview.md if applicable.",
            legacy_file.display()
        );
        return Ok(());
    }
    std::fs::remove_file(&legacy_file)?;
    if std::fs::read_dir(&legacy_dir)?.next().is_none() {
        std::fs::remove_dir(&legacy_dir)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn target_path(project_root: &Path, target: &SkillTarget) -> std::path::PathBuf {
        match target.kind {
            InstallKind::ClaudePlugin => project_root.join("skills/gobby/SKILL.md"),
            InstallKind::SkillDir { cli_dir } => {
                project_root.join(cli_dir).join("skills/gobby/SKILL.md")
            }
        }
    }

    fn expected_reported_path(target: &SkillTarget) -> String {
        match target.kind {
            InstallKind::ClaudePlugin => "skills/gobby/SKILL.md".to_string(),
            InstallKind::SkillDir { cli_dir } => format!("{cli_dir}/skills/gobby/SKILL.md"),
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
    fn supported_targets_are_stable() {
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

        assert_eq!(reported_path, "skills/gobby/SKILL.md");
        assert_eq!(manifest["name"], "gcode");
        assert_eq!(
            manifest["description"],
            "AST-aware code search, symbol navigation, and dependency graph analysis"
        );
        assert_eq!(manifest["version"], "0.1.0");
    }

    #[test]
    fn installing_skills_does_not_delete_existing_cli_files() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let sentinels = [
            ".codex/config.toml",
            ".factory/settings.json",
            ".grok/notes.md",
            ".qwen/state.json",
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

    #[test]
    fn upgrade_retires_only_owned_predecessor_and_keeps_custom_files() {
        let previous = include_str!("../tests/fixtures/retired-code-index-skill.md");
        for target in supported_targets() {
            let tmp = tempfile::tempdir().expect("tempdir");
            let destination = target_path(tmp.path(), target);
            let skills_dir = destination
                .parent()
                .expect("gobby dir")
                .parent()
                .expect("skills dir");
            let old = skills_dir.join("gcode");
            std::fs::create_dir_all(&old).expect("legacy dir");
            std::fs::write(old.join("SKILL.md"), previous).expect("legacy carrier");
            std::fs::write(old.join("user-notes.md"), "keep").expect("custom notes");
            install_skill(tmp.path(), target).expect("upgrade");
            assert!(!old.join("SKILL.md").exists());
            assert_eq!(
                std::fs::read_to_string(old.join("user-notes.md")).expect("notes"),
                "keep"
            );
            assert_eq!(
                std::fs::read_to_string(&destination).expect("router"),
                SKILL_CONTENT
            );
            install_skill(tmp.path(), target).expect("repeat upgrade");
            std::fs::write(old.join("SKILL.md"), "Customized instructions").expect("custom skill");
            install_skill(tmp.path(), target).expect("preserve custom");
            assert_eq!(
                std::fs::read_to_string(old.join("SKILL.md")).expect("custom skill"),
                "Customized instructions"
            );
        }
    }

    #[test]
    fn existing_custom_router_is_preserved() {
        for target in supported_targets() {
            let tmp = tempfile::tempdir().expect("tempdir");
            let destination = target_path(tmp.path(), target);
            std::fs::create_dir_all(destination.parent().expect("router parent")).expect("parent");
            std::fs::write(&destination, "Custom gobby router").expect("custom router");
            let error = install_skill(tmp.path(), target).expect_err("must preserve custom router");
            assert_eq!(error.kind(), std::io::ErrorKind::AlreadyExists);
            assert_eq!(
                std::fs::read_to_string(&destination).expect("custom router"),
                "Custom gobby router"
            );
            assert!(!tmp.path().join(".claude-plugin/plugin.json").exists());
        }
    }

    #[test]
    fn custom_plugin_manifest_is_preserved_before_installation() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let plugin_dir = tmp.path().join(".claude-plugin");
        std::fs::create_dir_all(&plugin_dir).expect("plugin directory");
        let manifest = plugin_dir.join("plugin.json");
        let custom = r#"{"name":"user-plugin","version":"2.0.0"}"#;
        std::fs::write(&manifest, custom).expect("custom manifest");
        let error = install_claude_plugin(tmp.path()).expect_err("custom manifest is preserved");
        assert_eq!(error.kind(), std::io::ErrorKind::AlreadyExists);
        assert_eq!(std::fs::read_to_string(manifest).expect("manifest"), custom);
        assert!(!tmp.path().join("skills/gobby").exists());
    }
}
