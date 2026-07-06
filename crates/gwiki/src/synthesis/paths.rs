use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};

use gobby_core::vault::reserved::{deconflict_reserved_slug, is_reserved_instruction_stem};
use serde_json::Value;

use crate::WikiError;
use crate::frontmatter::parse_frontmatter;

use super::types::{ArticleKind, SynthesisSource};

const MAX_SLUG_TRIES: usize = 500;

/// Frontmatter key on compile-emitted source stub pages recording the
/// vault-relative raw source path they were synthesized from. Recompiles use
/// it to resolve a source back to its existing page instead of minting a
/// slug-suffixed sibling (#17596).
pub(super) const SOURCE_PATH_FRONTMATTER_KEY: &str = "source_path";

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
    if let Some(stem) = relative.file_stem().and_then(|stem| stem.to_str())
        && is_reserved_instruction_stem(stem)
    {
        return Err(WikiError::InvalidInput {
            field,
            message: format!(
                "page filename `{stem}.md` collides with an agent instruction \
                 filename on case-insensitive filesystems"
            ),
        });
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

/// Derive a unique page slug for `title`. `reserved_suffix` is appended when
/// the slug would collide with an agent instruction filename (#17645) — pass
/// [`ArticleKind::reserved_suffix`] for the page's kind.
pub fn slugify_unique(
    title: &str,
    reserved_suffix: &str,
    mut exists: impl FnMut(&str) -> bool,
) -> String {
    let base = deconflict_reserved_slug(&slugify(title), reserved_suffix);
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
            if let Some(existing) = &source.existing_page {
                return existing.clone();
            }
            let identity = relative_path(vault_root, &source.path);
            let slug = slugify_unique(
                &source.title,
                ArticleKind::Source.reserved_suffix(),
                |slug| {
                    if reserved.contains(slug) {
                        return true;
                    }
                    let candidate = directory.join(format!("{slug}.md"));
                    // A page already emitted for this same source is not a
                    // collision: recompiles update it in place instead of
                    // minting a slug-suffixed sibling.
                    candidate.exists() && !page_matches_source_identity(&candidate, &identity)
                },
            );
            reserved.insert(slug.clone());
            directory.join(format!("{slug}.md"))
        })
        .collect()
}

/// True when the page at `page_path` is a compile-emitted stub for the source
/// identified by `identity` (its vault-relative raw path). Unreadable or
/// unparseable pages never match, so they keep colliding into fresh slugs.
fn page_matches_source_identity(page_path: &Path, identity: &str) -> bool {
    let Ok(markdown) = std::fs::read_to_string(page_path) else {
        return false;
    };
    parse_frontmatter(&markdown).is_ok_and(|parsed| {
        parsed
            .metadata
            .unknown
            .get(SOURCE_PATH_FRONTMATTER_KEY)
            .and_then(Value::as_str)
            == Some(identity)
    })
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
