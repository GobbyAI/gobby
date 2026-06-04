use std::fs;
use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::support::text::slugify_with_options;
use crate::support::time;

use super::*;

pub(crate) fn render_bundle(bundle: &CompileBundle) -> String {
    let mut rendered = String::new();
    rendered.push_str("# Compile bundle: ");
    rendered.push_str(&bundle.topic);
    rendered.push_str("\n\n");

    render_list_section(&mut rendered, "Topic outline", &bundle.outline);

    rendered.push_str("## Accepted sources\n\n");
    if bundle.accepted_sources.is_empty() {
        rendered.push_str("- None recorded.\n");
    } else {
        for source in &bundle.accepted_sources {
            rendered.push_str("- ");
            rendered.push_str(&source.title);
            rendered.push_str(" (");
            rendered.push_str(&source.path.to_string_lossy());
            rendered.push_str(")\n");
            for chunk in &source.chunks {
                rendered.push_str("  - ");
                rendered.push_str(chunk);
                rendered.push('\n');
            }
        }
    }
    rendered.push('\n');

    render_list_section(&mut rendered, "Citations", &bundle.citations);
    render_list_section(
        &mut rendered,
        "Conflicting claims",
        &bundle.conflicting_claims,
    );
    render_list_section(&mut rendered, "Missing evidence", &bundle.missing_evidence);

    rendered
}

fn render_list_section(rendered: &mut String, title: &str, values: &[String]) {
    rendered.push_str("## ");
    rendered.push_str(title);
    rendered.push_str("\n\n");
    if values.is_empty() {
        rendered.push_str("- None recorded.\n\n");
        return;
    }
    for value in values {
        rendered.push_str("- ");
        rendered.push_str(value);
        rendered.push('\n');
    }
    rendered.push('\n');
}

pub(crate) fn write_target_page(
    vault_root: &Path,
    target_page: &Path,
    rendered: &str,
) -> Result<(), WikiError> {
    if target_page.exists() {
        return Err(WikiError::InvalidInput {
            field: "write_intent",
            message: format!(
                "existing page {} requires merge/diff handling before overwrite",
                target_page.display()
            ),
        });
    }
    if let Some(parent) = target_page.parent() {
        ensure_compile_target_parent_inside_vault(vault_root, parent)?;
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create compile target directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    fs::write(target_page, rendered).map_err(|error| WikiError::Io {
        action: "write compile target page",
        path: Some(target_page.to_path_buf()),
        source: error,
    })
}

fn ensure_compile_target_parent_inside_vault(
    vault_root: &Path,
    parent: &Path,
) -> Result<(), WikiError> {
    let root = vault_root.canonicalize().map_err(|error| WikiError::Io {
        action: "resolve vault root",
        path: Some(vault_root.to_path_buf()),
        source: error,
    })?;
    let mut existing_parent = parent;
    while !existing_parent.exists() {
        existing_parent = existing_parent
            .parent()
            .ok_or_else(|| WikiError::InvalidInput {
                field: "target_page",
                message: "compile target page must stay inside the vault".to_string(),
            })?;
    }
    let parent = existing_parent
        .canonicalize()
        .map_err(|error| WikiError::Io {
            action: "resolve compile target directory",
            path: Some(existing_parent.to_path_buf()),
            source: error,
        })?;
    if parent.starts_with(root) {
        Ok(())
    } else {
        Err(WikiError::InvalidInput {
            field: "target_page",
            message: "compile target page must stay inside the vault".to_string(),
        })
    }
}

pub(crate) fn normalize_target_page(
    vault_root: &Path,
    target_page: Option<&Path>,
) -> Result<Option<PathBuf>, WikiError> {
    let Some(target_page) = target_page else {
        return Ok(None);
    };
    if target_page.is_absolute() {
        return Err(WikiError::InvalidInput {
            field: "target_page",
            message: "compile target page must be vault-relative".to_string(),
        });
    }

    let mut normalized = PathBuf::new();
    for component in target_page.components() {
        match component {
            std::path::Component::Normal(value) => normalized.push(value),
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir
            | std::path::Component::RootDir
            | std::path::Component::Prefix(_) => {
                return Err(WikiError::InvalidInput {
                    field: "target_page",
                    message: "compile target page must stay inside the vault".to_string(),
                });
            }
        }
    }
    if normalized.as_os_str().is_empty() {
        return Err(WikiError::InvalidInput {
            field: "target_page",
            message: "compile target page must identify a wiki document".to_string(),
        });
    }
    Ok(Some(vault_root.join(normalized)))
}

pub(crate) fn slugify(topic: &str) -> String {
    slugify_with_options(topic, Some("handoff"), None)
}

pub(crate) fn unix_timestamp_ms() -> Result<u64, WikiError> {
    time::unix_timestamp_ms()
}
