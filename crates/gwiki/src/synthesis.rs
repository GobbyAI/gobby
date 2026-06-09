use std::collections::HashSet;
use std::fs;
use std::io::Write;
use std::path::{Component, Path, PathBuf};

use serde::Serialize;

use crate::WikiError;

const MAX_SLUG_TRIES: usize = 500;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ArticleKind {
    Source,
    Concept,
    Topic,
}

impl ArticleKind {
    pub fn directory(self) -> &'static str {
        match self {
            Self::Source => "knowledge/sources",
            Self::Concept => "knowledge/concepts",
            Self::Topic => "knowledge/topics",
        }
    }

    pub fn source_kind(self) -> &'static str {
        match self {
            Self::Source => "source_note",
            Self::Concept => "concept",
            Self::Topic => "topic",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SynthesisSource {
    pub title: String,
    pub path: PathBuf,
    pub chunks: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SynthesisInput {
    pub handoff_id: String,
    pub topic: String,
    pub outline: Vec<String>,
    pub target_kind: ArticleKind,
    pub accepted_sources: Vec<SynthesisSource>,
    pub citations: Vec<String>,
    pub conflicting_claims: Vec<String>,
    pub missing_evidence: Vec<String>,
    pub daemon_synthesis_available: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SynthesisPrompt {
    pub system: String,
    pub user: String,
    pub daemon_synthesis_available: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SynthesizedPage {
    pub path: PathBuf,
    pub title: String,
    pub markdown: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WritePolicy {
    RequireMergeIntent,
    AllowOverwriteAfterMerge,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PageWriteKind {
    Created,
    Overwritten,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PageWriteOutcome {
    pub path: PathBuf,
    pub kind: PageWriteKind,
}

pub fn build_synthesis_prompt(input: &SynthesisInput) -> SynthesisPrompt {
    let synthesis_mode = if input.daemon_synthesis_available {
        "daemon completion endpoint is available"
    } else {
        "daemon completion endpoint is unavailable; use deterministic source excerpts"
    };
    let mut user = format!(
        "Compile topic: {}\nHandoff: {}\nMode: {synthesis_mode}\n",
        input.topic, input.handoff_id
    );
    if !input.outline.is_empty() {
        user.push_str("Outline:\n");
        for item in &input.outline {
            user.push_str("- ");
            user.push_str(item);
            user.push('\n');
        }
    }
    user.push_str("Accepted sources:\n");
    for source in &input.accepted_sources {
        user.push_str("- ");
        user.push_str(&source.title);
        user.push_str(" (");
        user.push_str(&source.path.to_string_lossy());
        user.push_str(")\n");
    }

    SynthesisPrompt {
        system: "Create Obsidian-compatible markdown grounded only in accepted sources."
            .to_string(),
        user,
        daemon_synthesis_available: input.daemon_synthesis_available,
    }
}

pub fn synthesize_article(
    vault_root: &Path,
    input: &SynthesisInput,
    target_page: Option<PathBuf>,
) -> Result<SynthesizedPage, WikiError> {
    let path = target_page.unwrap_or_else(|| {
        let directory = vault_root.join(input.target_kind.directory());
        let slug = slugify_unique(&input.topic, |slug| {
            directory.join(format!("{slug}.md")).exists()
        });
        directory.join(format!("{slug}.md"))
    });
    ensure_synthesized_path_inside_vault(vault_root, &path, "article_path")?;
    let mut markdown = String::new();
    render_frontmatter(
        &mut markdown,
        &input.topic,
        input.target_kind.source_kind(),
        &input.handoff_id,
        if input.daemon_synthesis_available {
            "daemon"
        } else {
            "fallback"
        },
    );
    markdown.push_str("# ");
    markdown.push_str(&input.topic);
    markdown.push_str("\n\n");

    let source_paths = source_page_paths(vault_root, &path, &input.accepted_sources);
    let source_links = source_links(vault_root, &input.accepted_sources, &source_paths);
    if !source_links.is_empty() {
        markdown.push_str("Sources: ");
        markdown.push_str(&source_links.join(", "));
        markdown.push_str("\n\n");
    }

    let headings = if input.outline.is_empty() {
        vec!["Overview".to_string()]
    } else {
        input.outline.clone()
    };
    for heading in &headings {
        markdown.push_str("## ");
        markdown.push_str(heading);
        markdown.push_str("\n\n");
    }
    markdown.push_str("## Source excerpts\n\n");
    render_source_excerpts(&mut markdown, &input.accepted_sources);

    render_list_section(&mut markdown, "Citations", &input.citations);
    render_list_section(
        &mut markdown,
        "Conflicting claims",
        &input.conflicting_claims,
    );
    render_list_section(&mut markdown, "Missing evidence", &input.missing_evidence);
    if !source_links.is_empty() {
        render_list_section(&mut markdown, "Backlinks", &source_links);
    }

    Ok(SynthesizedPage {
        path,
        title: input.topic.clone(),
        markdown,
    })
}

pub fn synthesize_source_pages(
    vault_root: &Path,
    input: &SynthesisInput,
    article_path: &Path,
) -> Result<Vec<SynthesizedPage>, WikiError> {
    let article_link = wiki_link(vault_root, article_path, &input.topic);
    let source_paths = source_page_paths(vault_root, article_path, &input.accepted_sources);
    let mut pages = Vec::with_capacity(input.accepted_sources.len());
    for (source, path) in input.accepted_sources.iter().zip(source_paths) {
        ensure_synthesized_path_inside_vault(vault_root, &path, "source_path")?;
        let mut markdown = String::new();
        render_frontmatter(
            &mut markdown,
            &source.title,
            ArticleKind::Source.source_kind(),
            &input.handoff_id,
            "source",
        );
        markdown.push_str("# ");
        markdown.push_str(&source.title);
        markdown.push_str("\n\n");
        markdown.push_str("Source path: `");
        markdown.push_str(&relative_path(vault_root, &source.path));
        markdown.push_str("`\n\n");
        render_list_section(&mut markdown, "Extracts", &source.chunks);
        render_list_section(
            &mut markdown,
            "Used by",
            std::slice::from_ref(&article_link),
        );

        pages.push(SynthesizedPage {
            path,
            title: source.title.clone(),
            markdown,
        });
    }
    Ok(pages)
}

/// Advisory preflight for callers that want to fail before expensive synthesis.
///
/// The actual race-free protection lives in `write_synthesized_page`, which
/// uses `create_new` for `RequireMergeIntent` and atomic replacement for
/// overwrite-after-merge writes.
pub fn ensure_page_write_allowed(
    page: &SynthesizedPage,
    policy: WritePolicy,
) -> Result<(), WikiError> {
    if page.path.exists() && policy == WritePolicy::RequireMergeIntent {
        return Err(WikiError::InvalidInput {
            field: "write_intent",
            message: format!(
                "existing page {} requires merge/diff handling before overwrite",
                page.path.display()
            ),
        });
    }
    Ok(())
}

pub fn write_synthesized_page(
    vault_root: &Path,
    page: &SynthesizedPage,
    policy: WritePolicy,
) -> Result<PageWriteOutcome, WikiError> {
    ensure_synthesized_path_inside_vault(vault_root, &page.path, "synthesized_page")?;
    if let Some(parent) = page.path.parent() {
        ensure_existing_parent_inside_vault(vault_root, parent, "synthesized_page")?;
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create synthesized page directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }

    let kind = match policy {
        WritePolicy::RequireMergeIntent => {
            let file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&page.path)
                .map_err(|error| {
                    if error.kind() == std::io::ErrorKind::AlreadyExists {
                        WikiError::InvalidInput {
                            field: "write_intent",
                            message: format!(
                                "existing page {} requires merge/diff handling before overwrite",
                                page.path.display()
                            ),
                        }
                    } else {
                        WikiError::Io {
                            action: "create synthesized wiki page",
                            path: Some(page.path.clone()),
                            source: error,
                        }
                    }
                })?;
            write_created_synthesized_page(file, &page.path, page.markdown.as_bytes())?;
            sync_parent_dir(&page.path)?;
            PageWriteKind::Created
        }
        WritePolicy::AllowOverwriteAfterMerge => {
            match fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&page.path)
            {
                Ok(file) => {
                    write_created_synthesized_page(file, &page.path, page.markdown.as_bytes())?;
                    sync_parent_dir(&page.path)?;
                    PageWriteKind::Created
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    write_synthesized_page_atomically(&page.path, page.markdown.as_bytes())?;
                    PageWriteKind::Overwritten
                }
                Err(error) => {
                    return Err(WikiError::Io {
                        action: "create synthesized wiki page",
                        path: Some(page.path.clone()),
                        source: error,
                    });
                }
            }
        }
    };
    Ok(PageWriteOutcome {
        path: page.path.clone(),
        kind,
    })
}

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

fn ensure_existing_parent_inside_vault(
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

fn source_page_paths(
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

fn source_links(
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

fn render_frontmatter(
    markdown: &mut String,
    title: &str,
    source_kind: &str,
    handoff_id: &str,
    synthesis_mode: &str,
) {
    markdown.push_str("---\n");
    markdown.push_str("title: ");
    markdown.push_str(&yaml_scalar(title));
    markdown.push('\n');
    markdown.push_str("source_kind: ");
    markdown.push_str(&yaml_scalar(source_kind));
    markdown.push('\n');
    markdown.push_str("tags:\n");
    markdown.push_str("  - gwiki\n");
    markdown.push_str("  - compiled\n");
    markdown.push_str("compile_handoff: ");
    markdown.push_str(&yaml_scalar(handoff_id));
    markdown.push('\n');
    markdown.push_str("synthesis_mode: ");
    markdown.push_str(&yaml_scalar(synthesis_mode));
    markdown.push_str("\n---\n\n");
}

fn render_source_excerpts(markdown: &mut String, sources: &[SynthesisSource]) {
    if sources.is_empty() {
        markdown.push_str("No accepted sources were recorded.\n\n");
        return;
    }

    for source in sources {
        markdown.push_str("- ");
        markdown.push_str(&source.title);
        markdown.push_str(": ");
        if let Some(chunk) = source.chunks.first() {
            markdown.push_str(chunk);
        } else {
            markdown.push_str("Accepted source has no extracted body text.");
        }
        markdown.push('\n');
    }
    markdown.push('\n');
}

fn render_list_section(markdown: &mut String, title: &str, values: &[String]) {
    markdown.push_str("## ");
    markdown.push_str(title);
    markdown.push_str("\n\n");
    if values.is_empty() {
        markdown.push_str("- None recorded.\n\n");
        return;
    }
    for value in values {
        markdown.push_str("- ");
        markdown.push_str(value);
        markdown.push('\n');
    }
    markdown.push('\n');
}

fn trim_markdown_extension(path: &str) -> String {
    path.strip_suffix(".md")
        .or_else(|| path.strip_suffix(".markdown"))
        .unwrap_or(path)
        .to_string()
}

fn yaml_scalar(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\\' => escaped.push_str("\\\\"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            '\0' => escaped.push_str("\\u0000"),
            '\u{0008}' => escaped.push_str("\\b"),
            '\u{000C}' => escaped.push_str("\\f"),
            '\u{007F}' => escaped.push_str("\\u007f"),
            ch if ch.is_control() => escaped.push_str(&format!("\\u{:04x}", ch as u32)),
            ch => escaped.push(ch),
        }
    }
    format!("\"{escaped}\"")
}

fn write_created_synthesized_page(
    mut file: fs::File,
    path: &Path,
    contents: &[u8],
) -> Result<(), WikiError> {
    if let Err(error) = file.write_all(contents) {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(WikiError::Io {
            action: "write synthesized wiki page",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(WikiError::Io {
            action: "write synthesized wiki page",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    Ok(())
}

fn write_synthesized_page_atomically(path: &Path, contents: &[u8]) -> Result<(), WikiError> {
    let temp_path = temp_sibling_path(path);
    let mut file = fs::File::create(&temp_path).map_err(|error| WikiError::Io {
        action: "create synthesized page temp file",
        path: Some(temp_path.clone()),
        source: error,
    })?;
    if let Err(error) = file.write_all(contents) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write synthesized page temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync synthesized page temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = fs::rename(&temp_path, path) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "replace synthesized wiki page",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    sync_parent_dir(path)?;
    Ok(())
}

fn sync_parent_dir(path: &Path) -> Result<(), WikiError> {
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok(())
    }
    #[cfg(unix)]
    {
        let Some(parent) = path.parent() else {
            return Ok(());
        };
        fs::File::open(parent)
            .and_then(|dir| dir.sync_all())
            .map_err(|error| WikiError::Io {
                action: "sync synthesized page directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })
    }
}

fn temp_sibling_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("page.md");
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    path.with_file_name(format!(".{file_name}.{}.{nanos}.tmp", std::process::id()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn existing_page_requires_merge_intent() {
        let temp = tempfile::tempdir().expect("tempdir");
        let page_path = temp.path().join("knowledge/topics/existing.md");
        std::fs::create_dir_all(page_path.parent().expect("page parent")).expect("create parent");
        std::fs::write(&page_path, "human-authored page").expect("existing page written");

        let page = SynthesizedPage {
            path: page_path.clone(),
            title: "Existing".to_string(),
            markdown: "---\ntitle: Existing\n---\n# Existing\nNew synthesis.\n".to_string(),
        };

        let error = write_synthesized_page(temp.path(), &page, WritePolicy::RequireMergeIntent)
            .expect_err("existing page requires merge intent");

        assert!(matches!(
            error,
            crate::WikiError::InvalidInput {
                field: "write_intent",
                ..
            }
        ));
        assert_eq!(
            std::fs::read_to_string(&page_path).expect("page retained"),
            "human-authored page"
        );
    }

    #[test]
    fn synthesized_page_write_classifies_create_and_overwrite_atomically() {
        let temp = tempfile::tempdir().expect("tempdir");
        let page_path = temp.path().join("knowledge/topics/new.md");
        let page = SynthesizedPage {
            path: page_path.clone(),
            title: "New".to_string(),
            markdown: "# New\n".to_string(),
        };

        let created =
            write_synthesized_page(temp.path(), &page, WritePolicy::AllowOverwriteAfterMerge)
                .expect("create synthesized page");
        let overwritten =
            write_synthesized_page(temp.path(), &page, WritePolicy::AllowOverwriteAfterMerge)
                .expect("overwrite synthesized page");

        assert_eq!(created.kind, PageWriteKind::Created);
        assert_eq!(overwritten.kind, PageWriteKind::Overwritten);
        assert_eq!(
            std::fs::read_to_string(&page_path).expect("page written"),
            "# New\n"
        );
    }

    #[test]
    fn slugify_unique_falls_back_after_bounded_suffixes() {
        let slug = slugify_unique("Collision", |_| true);

        assert!(slug.starts_with("collision-"));
        assert!(slug.len() > "collision-".len());
    }

    #[test]
    fn source_page_paths_reserve_article_path() {
        let temp = tempfile::tempdir().expect("tempdir");
        let article_path = temp.path().join("knowledge/sources/collision.md");
        let sources = vec![SynthesisSource {
            title: "Collision".to_string(),
            path: PathBuf::from("raw/collision.md"),
            chunks: Vec::new(),
        }];

        let paths = source_page_paths(temp.path(), &article_path, &sources);

        assert_ne!(paths[0], article_path);
        assert!(paths[0].starts_with(temp.path().join("knowledge/sources")));
    }

    #[test]
    fn synthesized_article_rejects_escaping_target_path() {
        let temp = tempfile::tempdir().expect("tempdir");
        let input = SynthesisInput {
            handoff_id: "handoff-1".to_string(),
            topic: "Escape".to_string(),
            outline: vec![],
            target_kind: ArticleKind::Topic,
            accepted_sources: vec![],
            citations: vec![],
            conflicting_claims: vec![],
            missing_evidence: vec![],
            daemon_synthesis_available: false,
        };
        let outside_name = format!(
            "{}-outside.md",
            temp.path()
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("synthesis")
        );
        let target = temp.path().join("..").join(outside_name);

        let error = synthesize_article(temp.path(), &input, Some(target))
            .expect_err("escaping target must be rejected");

        assert!(matches!(
            error,
            WikiError::InvalidInput {
                field: "article_path",
                ..
            }
        ));
    }

    #[test]
    fn synthesized_writer_rejects_escaping_page_path_before_write() {
        let temp = tempfile::tempdir().expect("tempdir");
        let outside_name = format!(
            "{}-outside.md",
            temp.path()
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("synthesis")
        );
        let outside = temp.path().join("..").join(outside_name);
        let page = SynthesizedPage {
            path: outside.clone(),
            title: "Outside".to_string(),
            markdown: "# Outside\n".to_string(),
        };

        let error =
            write_synthesized_page(temp.path(), &page, WritePolicy::AllowOverwriteAfterMerge)
                .expect_err("escaping page must be rejected");

        assert!(matches!(
            error,
            WikiError::InvalidInput {
                field: "synthesized_page",
                ..
            }
        ));
        assert!(!outside.exists());
    }

    #[test]
    #[cfg(unix)]
    fn synthesized_writer_rejects_symlinked_parent_before_create_dir_all() {
        use std::os::unix::fs::symlink;

        let temp = tempfile::tempdir().expect("tempdir");
        let outside = tempfile::tempdir().expect("outside tempdir");
        let link = temp.path().join("wiki").join("linked");
        fs::create_dir_all(link.parent().expect("link parent")).expect("link parent");
        symlink(outside.path(), &link).expect("symlink parent");
        let page = SynthesizedPage {
            path: link.join("nested/page.md"),
            title: "Outside".to_string(),
            markdown: "# Outside\n".to_string(),
        };

        let error =
            write_synthesized_page(temp.path(), &page, WritePolicy::AllowOverwriteAfterMerge)
                .expect_err("symlinked parent must be rejected");

        assert!(matches!(
            error,
            WikiError::InvalidInput {
                field: "synthesized_page",
                ..
            }
        ));
        assert!(!outside.path().join("nested/page.md").exists());
    }

    #[test]
    fn yaml_scalar_escapes_quoted_control_characters() {
        assert_eq!(yaml_scalar("Plain Title"), "\"Plain Title\"");
        assert_eq!(
            yaml_scalar("a\\b\"c\nd\re\tf"),
            "\"a\\\\b\\\"c\\nd\\re\\tf\""
        );
        assert_eq!(
            yaml_scalar("nul\0del\u{7f}\u{80}"),
            "\"nul\\u0000del\\u007f\\u0080\""
        );
    }
}
