use std::collections::HashSet;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::WikiError;

const MAX_SLUG_TRIES: usize = 10_000;

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
            Self::Source => "wiki/sources",
            Self::Concept => "wiki/concepts",
            Self::Topic => "wiki/topics",
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
) -> SynthesizedPage {
    let path = target_page.unwrap_or_else(|| {
        let directory = vault_root.join(input.target_kind.directory());
        let slug = slugify_unique(&input.topic, |slug| {
            directory.join(format!("{slug}.md")).exists()
        });
        directory.join(format!("{slug}.md"))
    });
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

    let source_paths = source_page_paths(vault_root, &input.accepted_sources);
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

    SynthesizedPage {
        path,
        title: input.topic.clone(),
        markdown,
    }
}

pub fn synthesize_source_pages(
    vault_root: &Path,
    input: &SynthesisInput,
    article_path: &Path,
) -> Vec<SynthesizedPage> {
    let article_link = wiki_link(vault_root, article_path, &input.topic);
    let source_paths = source_page_paths(vault_root, &input.accepted_sources);
    input
        .accepted_sources
        .iter()
        .zip(source_paths)
        .map(|(source, path)| {
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

            SynthesizedPage {
                path,
                title: source.title.clone(),
                markdown,
            }
        })
        .collect()
}

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
    page: &SynthesizedPage,
    policy: WritePolicy,
) -> Result<PageWriteOutcome, WikiError> {
    if let Some(parent) = page.path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create synthesized page directory",
            path: Some(parent.to_path_buf()),
            source: error.to_string(),
        })?;
    }

    let kind = match policy {
        WritePolicy::RequireMergeIntent => {
            let mut file = fs::OpenOptions::new()
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
                            source: error.to_string(),
                        }
                    }
                })?;
            file.write_all(page.markdown.as_bytes())
                .map_err(|error| WikiError::Io {
                    action: "write synthesized wiki page",
                    path: Some(page.path.clone()),
                    source: error.to_string(),
                })?;
            PageWriteKind::Created
        }
        WritePolicy::AllowOverwriteAfterMerge => {
            let kind = if page.path.exists() {
                PageWriteKind::Overwritten
            } else {
                PageWriteKind::Created
            };
            fs::write(&page.path, &page.markdown).map_err(|error| WikiError::Io {
                action: "write synthesized wiki page",
                path: Some(page.path.clone()),
                source: error.to_string(),
            })?;
            kind
        }
    };
    Ok(PageWriteOutcome {
        path: page.path.clone(),
        kind,
    })
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

fn source_page_paths(vault_root: &Path, sources: &[SynthesisSource]) -> Vec<PathBuf> {
    let directory = vault_root.join(ArticleKind::Source.directory());
    let mut reserved = HashSet::new();
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
    markdown.push_str(source_kind);
    markdown.push('\n');
    markdown.push_str("tags:\n");
    markdown.push_str("  - gwiki\n");
    markdown.push_str("  - compiled\n");
    markdown.push_str("compile_handoff: ");
    markdown.push_str(&yaml_scalar(handoff_id));
    markdown.push('\n');
    markdown.push_str("synthesis_mode: ");
    markdown.push_str(synthesis_mode);
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
    let escaped = value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('\t', "\\t");
    format!("\"{escaped}\"")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn existing_page_requires_merge_intent() {
        let temp = tempfile::tempdir().expect("tempdir");
        let page_path = temp.path().join("wiki/topics/existing.md");
        std::fs::create_dir_all(page_path.parent().expect("page parent")).expect("create parent");
        std::fs::write(&page_path, "human-authored page").expect("existing page written");

        let page = SynthesizedPage {
            path: page_path.clone(),
            title: "Existing".to_string(),
            markdown: "---\ntitle: Existing\n---\n# Existing\nNew synthesis.\n".to_string(),
        };

        let error = write_synthesized_page(&page, WritePolicy::RequireMergeIntent)
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
    fn slugify_unique_falls_back_after_bounded_suffixes() {
        let slug = slugify_unique("Collision", |_| true);

        assert!(slug.starts_with("collision-"));
        assert!(slug.len() > "collision-".len());
    }

    #[test]
    fn yaml_scalar_escapes_quoted_control_characters() {
        assert_eq!(yaml_scalar("Plain Title"), "\"Plain Title\"");
        assert_eq!(
            yaml_scalar("a\\b\"c\nd\re\tf"),
            "\"a\\\\b\\\"c\\nd\\re\\tf\""
        );
    }
}
