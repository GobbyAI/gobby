use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};

use crate::WikiError;

use super::types::{ArticleKind, SynthesisSource};

const MAX_SLUG_TRIES: usize = 500;

pub fn ensure_synthesized_path_inside_vault(
    vault_root: &Path,
    path: &Path,
    field: &'static str,
) -> Result<(), WikiError> {
    let root = vault_root.canonicalize().map_err(|error| WikiError::Io {
        action: "resolve vault root",
        path: Some(vault_root.to_path_buf()),
        source: error,
    })?;
    let candidate = if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    };
    let candidate = canonicalize_existing_prefix(&candidate, "resolve synthesized path")?;
    let Ok(relative) = candidate.strip_prefix(&root) else {
        return Err(synthesized_path_outside_vault(field));
    };
    if relative.components().any(|component| {
        matches!(
            component,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        )
    }) {
        return Err(synthesized_path_outside_vault(field));
    }
    Ok(())
}

fn canonicalize_existing_prefix(path: &Path, action: &'static str) -> Result<PathBuf, WikiError> {
    let mut current = path;
    let mut missing_suffix = Vec::new();
    while !current.exists() {
        let Some(name) = current.file_name() else {
            break;
        };
        missing_suffix.push(name.to_os_string());
        let Some(parent) = current.parent() else {
            break;
        };
        current = parent;
    }

    let mut resolved = current.canonicalize().map_err(|error| WikiError::Io {
        action,
        path: Some(current.to_path_buf()),
        source: error,
    })?;
    for component in missing_suffix.iter().rev() {
        resolved.push(component);
    }
    Ok(resolved)
}

pub(super) fn ensure_existing_parent_inside_vault(
    vault_root: &Path,
    parent: &Path,
    field: &'static str,
) -> Result<(), WikiError> {
    let root = vault_root.canonicalize().map_err(|error| WikiError::Io {
        action: "resolve vault root",
        path: Some(vault_root.to_path_buf()),
        source: error,
    })?;
    let parent = canonicalize_existing_prefix(parent, "resolve synthesized page directory")?;
    if parent.starts_with(root) {
        return Ok(());
    }
    Err(synthesized_path_outside_vault(field))
}

fn synthesized_path_outside_vault(field: &'static str) -> WikiError {
    WikiError::InvalidInput {
        field,
        message: "synthesized wiki page path must stay inside the vault".to_string(),
    }
}

pub fn wiki_link(vault_root: &Path, path: &Path, title: &str) -> String {
    format!(
        "[[{}|{}]]",
        trim_markdown_extension(&relative_path(vault_root, path)),
        title
    )
}

pub fn slugify(title: &str) -> String {
    let mut slug = String::new();
    let mut last_was_dash = false;

    for ch in title.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            last_was_dash = false;
        } else if !last_was_dash && !slug.is_empty() {
            slug.push('-');
            last_was_dash = true;
        }
    }

    while slug.ends_with('-') {
        slug.pop();
    }

    if slug.is_empty() {
        "wiki-page".to_string()
    } else {
        slug
    }
}

pub fn slugify_unique(title: &str, mut exists: impl FnMut(&str) -> bool) -> String {
    let base = slugify(title);
    if !exists(&base) {
        return base;
    }

    for index in 2usize..=MAX_SLUG_TRIES {
        let candidate = format!("{base}-{index}");
        if !exists(&candidate) {
            return candidate;
        }
    }

    format!("{base}-{}", uuid::Uuid::new_v4().simple())
}

pub fn relative_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

pub(super) fn source_page_paths(
    vault_root: &Path,
    article_path: &Path,
    sources: &[SynthesisSource],
) -> Vec<PathBuf> {
    let directory = vault_root.join(ArticleKind::Source.directory());
    let mut reserved = HashSet::new();
    if article_path.parent() == Some(directory.as_path())
        && let Some(slug) = article_path.file_stem().and_then(|value| value.to_str())
    {
        reserved.insert(slug.to_string());
    }
    sources
        .iter()
        .map(|source| {
            let slug = slugify_unique(&source.title, |slug| {
                reserved.contains(slug) || directory.join(format!("{slug}.md")).exists()
            });
            reserved.insert(slug.clone());
            directory.join(format!("{slug}.md"))
        })
        .collect()
}

pub(super) fn source_links(
    vault_root: &Path,
    sources: &[SynthesisSource],
    source_paths: &[PathBuf],
) -> Vec<String> {
    sources
        .iter()
        .zip(source_paths)
        .map(|(source, path)| wiki_link(vault_root, path, &source.title))
        .collect()
}

fn trim_markdown_extension(path: &str) -> String {
    path.strip_suffix(".md")
        .or_else(|| path.strip_suffix(".markdown"))
        .unwrap_or(path)
        .to_string()
}
