use std::fmt;
use std::path::{Path, PathBuf};

use gobby_core::indexing::Chunk;
use serde_json::json;

use crate::frontmatter::{FrontmatterError, WikiFrontmatter, parse_frontmatter};
use crate::links::{WikiLink, extract_links};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarkdownHeading {
    pub level: u8,
    pub title: String,
    pub path: Vec<String>,
    pub byte_start: usize,
    pub byte_end: usize,
    pub section_byte_start: usize,
    pub section_byte_end: usize,
}

#[derive(Debug, Clone, PartialEq)]
pub struct MarkdownDomainRecord {
    pub path: PathBuf,
    pub frontmatter: WikiFrontmatter,
    pub body_start: usize,
    pub headings: Vec<MarkdownHeading>,
    pub links: Vec<WikiLink>,
    pub chunks: Vec<Chunk>,
}

#[derive(Debug)]
pub enum MarkdownParseError {
    Frontmatter(FrontmatterError),
    Io(std::io::Error),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct MarkdownFence {
    marker: u8,
    len: usize,
}

pub(crate) fn markdown_fence_start(line: &str) -> Option<MarkdownFence> {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return None;
    }
    let trimmed = &line[leading_spaces..];
    let marker = match trimmed.as_bytes().first().copied()? {
        b'`' | b'~' => trimmed.as_bytes()[0],
        _ => return None,
    };
    let len = trimmed.bytes().take_while(|byte| *byte == marker).count();
    (len >= 3).then_some(MarkdownFence { marker, len })
}

pub(crate) fn markdown_fence_closes(line: &str, fence: MarkdownFence) -> bool {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return false;
    }
    let trimmed = &line[leading_spaces..];
    let len = trimmed
        .bytes()
        .take_while(|byte| *byte == fence.marker)
        .count();
    len >= fence.len && trimmed[len..].trim().is_empty()
}

/// Normalize generated Markdown so it satisfies the whitespace family of
/// markdownlint rules without reflowing prose or altering meaningful content.
///
/// Guaranteed rules:
/// - **MD009** strip end-of-line whitespace (outside fenced code)
/// - **MD012** collapse runs of blank lines to a single blank line
/// - **MD022** surround ATX headings with a blank line
/// - **MD031** surround fenced code blocks with a blank line
/// - **MD047** end the file with exactly one trailing newline
///
/// The pass is fence-aware (content inside ` ``` ` / `~~~` fences is preserved
/// byte for byte) and frontmatter-aware (a leading YAML `---` / TOML `+++` block
/// is emitted verbatim). It never edits within a line, so table-cell `|` content
/// and `[[wikilink]]` text are left intact. It is idempotent:
/// `normalize(normalize(x)) == normalize(x)`.
pub(crate) fn normalize(input: &str) -> String {
    gobby_core::markdown::normalize_markdown(input)
}

impl fmt::Display for MarkdownParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Frontmatter(error) => write!(f, "{error}"),
            Self::Io(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for MarkdownParseError {}

impl From<FrontmatterError> for MarkdownParseError {
    fn from(error: FrontmatterError) -> Self {
        Self::Frontmatter(error)
    }
}

impl From<std::io::Error> for MarkdownParseError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

pub fn parse_markdown<I, S>(
    path: impl Into<PathBuf>,
    markdown: &str,
    known_targets: I,
) -> Result<MarkdownDomainRecord, MarkdownParseError>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let path = path.into();
    let frontmatter = parse_frontmatter(markdown)?;
    let links = extract_links(markdown, known_targets);
    let headings = extract_headings(markdown, frontmatter.body_start);
    let chunks = build_chunks(&path, markdown, frontmatter.body_start, &headings);

    Ok(MarkdownDomainRecord {
        path,
        frontmatter: frontmatter.metadata,
        body_start: frontmatter.body_start,
        headings,
        links,
        chunks,
    })
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn parse_index_file<I, S>(
    path: impl AsRef<Path>,
    known_targets: I,
) -> Result<MarkdownDomainRecord, MarkdownParseError>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let path = path.as_ref();
    let markdown = std::fs::read_to_string(path)?;
    parse_markdown(path.to_path_buf(), &markdown, known_targets)
}

fn extract_headings(markdown: &str, body_start: usize) -> Vec<MarkdownHeading> {
    let mut headings = Vec::new();
    let mut heading_path = Vec::new();
    let mut offset = body_start;
    let mut fence: Option<MarkdownFence> = None;

    while offset < markdown.len() {
        let line_end = markdown[offset..]
            .find('\n')
            .map_or(markdown.len(), |relative| offset + relative);
        let line_content_end = markdown[..line_end]
            .strip_suffix('\r')
            .map_or(line_end, |line| line.len());
        let line = &markdown[offset..line_content_end];

        if let Some(active_fence) = fence {
            if markdown_fence_closes(line, active_fence) {
                fence = None;
            }
        } else if let Some(opening_fence) = markdown_fence_start(line) {
            fence = Some(opening_fence);
        } else if let Some((level, title)) = parse_atx_heading(line) {
            let parent_depth = usize::from(level.saturating_sub(1));
            heading_path.truncate(parent_depth.min(heading_path.len()));
            heading_path.push(title.clone());
            headings.push(MarkdownHeading {
                level,
                title,
                path: heading_path.clone(),
                byte_start: offset,
                byte_end: line_content_end,
                section_byte_start: offset,
                section_byte_end: markdown.len(),
            });
        }

        if line_end == markdown.len() {
            break;
        }
        offset = line_end + 1;
    }

    let section_ends: Vec<usize> = headings
        .iter()
        .skip(1)
        .map(|heading| heading.byte_start)
        .chain(std::iter::once(markdown.len()))
        .collect();
    for (heading, section_end) in headings.iter_mut().zip(section_ends) {
        heading.section_byte_end = section_end;
    }

    headings
}

pub(crate) fn parse_atx_heading(line: &str) -> Option<(u8, String)> {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return None;
    }

    let line = &line[leading_spaces..];
    let level = line.bytes().take_while(|byte| *byte == b'#').count();
    if !(1..=6).contains(&level) {
        return None;
    }

    let after_marks = &line[level..];
    if !after_marks.is_empty() && !after_marks.chars().next().is_some_and(char::is_whitespace) {
        return None;
    }

    let title = strip_atx_closing_sequence(after_marks.trim()).to_string();
    Some((level as u8, title))
}

fn strip_atx_closing_sequence(title: &str) -> &str {
    let mut hash_start = title.len();
    let mut saw_hash = false;
    for (index, ch) in title.char_indices().rev() {
        if ch == '#' {
            saw_hash = true;
            hash_start = index;
        } else {
            break;
        }
    }
    if saw_hash
        && hash_start > 0
        && title[..hash_start]
            .chars()
            .next_back()
            .is_some_and(char::is_whitespace)
    {
        title[..hash_start].trim_end()
    } else {
        title
    }
}

fn build_chunks(
    path: &Path,
    markdown: &str,
    body_start: usize,
    headings: &[MarkdownHeading],
) -> Vec<Chunk> {
    let mut chunks = Vec::new();
    let mut next_start = body_start;

    for heading in headings {
        push_chunk(
            &mut chunks,
            path,
            markdown,
            next_start,
            heading.byte_start,
            None,
            Vec::new(),
        );
        push_chunk(
            &mut chunks,
            path,
            markdown,
            heading.section_byte_start,
            heading.section_byte_end,
            Some(heading.title.clone()),
            heading.path.clone(),
        );
        next_start = heading.section_byte_end;
    }

    push_chunk(
        &mut chunks,
        path,
        markdown,
        next_start,
        markdown.len(),
        None,
        Vec::new(),
    );

    chunks
}

fn push_chunk(
    chunks: &mut Vec<Chunk>,
    path: &Path,
    markdown: &str,
    byte_start: usize,
    byte_end: usize,
    heading: Option<String>,
    heading_path: Vec<String>,
) {
    if byte_start >= byte_end || markdown[byte_start..byte_end].trim().is_empty() {
        return;
    }

    chunks.push(Chunk {
        file_path: path.to_path_buf(),
        byte_start,
        byte_end,
        heading,
        metadata: json!({ "heading_path": heading_path }),
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    #[test]
    fn extracts_heading_ranges() {
        let markdown = concat!(
            "---\n",
            "title: Parser Test\n",
            "---\n",
            "Intro text.\n",
            "\n",
            "# Overview\n",
            "Overview body with [[Known]].\n",
            "\n",
            "# Details\n",
            "Details body.\n",
        );
        let overview_start = markdown.find("# Overview").expect("overview offset");
        let details_start = markdown.find("# Details").expect("details offset");

        let parsed = parse_markdown("knowledge/topics/parser.md", markdown, ["Known"])
            .expect("parse markdown");

        assert_eq!(
            parsed.body_start,
            markdown.find("Intro text.").expect("body offset")
        );
        assert_eq!(parsed.headings.len(), 2);
        assert_eq!(parsed.headings[0].title, "Overview");
        assert_eq!(parsed.headings[0].path, vec!["Overview"]);
        assert_eq!(parsed.headings[0].byte_start, overview_start);
        assert_eq!(parsed.headings[0].section_byte_start, overview_start);
        assert_eq!(parsed.headings[0].section_byte_end, details_start);
        assert_eq!(parsed.headings[1].title, "Details");
        assert_eq!(parsed.headings[1].section_byte_start, details_start);
        assert_eq!(parsed.headings[1].section_byte_end, markdown.len());

        assert_eq!(parsed.chunks.len(), 3);
        assert_eq!(parsed.chunks[0].heading, None);
        assert_eq!(parsed.chunks[0].byte_start, parsed.body_start);
        assert_eq!(parsed.chunks[0].byte_end, overview_start);
        assert_eq!(parsed.chunks[1].heading.as_deref(), Some("Overview"));
        assert_eq!(parsed.chunks[1].byte_start, overview_start);
        assert_eq!(parsed.chunks[1].byte_end, details_start);
        assert_eq!(
            parsed.chunks[1]
                .metadata
                .get("heading_path")
                .and_then(Value::as_array)
                .and_then(|path| path.first())
                .and_then(Value::as_str),
            Some("Overview")
        );
    }

    #[test]
    fn headings_ignore_code_until_matching_fence_length_closes() {
        let markdown = "````md\n# Not Heading\n```\n# Still Not Heading\n````\n# Heading\n";

        let parsed = parse_markdown(
            "knowledge/topics/fences.md",
            markdown,
            std::iter::empty::<&str>(),
        )
        .expect("parse markdown");

        assert_eq!(parsed.headings.len(), 1);
        assert_eq!(parsed.headings[0].title, "Heading");
    }

    #[test]
    fn index_parse_is_read_only() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let page = tmp.path().join("knowledge/topics/Page.md");
        std::fs::create_dir_all(page.parent().expect("parent")).expect("create parent");
        let markdown = "---\ntitle: Page\n---\n# Page\nSee [[Other Page]].\n";
        std::fs::write(&page, markdown).expect("write page");

        let parsed = parse_index_file(&page, ["Other Page"]).expect("parse index file");

        assert_eq!(parsed.path, page);
        assert_eq!(parsed.frontmatter.title.as_deref(), Some("Page"));
        assert_eq!(parsed.links.len(), 1);
        assert_eq!(
            std::fs::read_to_string(&parsed.path).expect("read page"),
            markdown
        );
    }

    #[test]
    fn atx_heading_keeps_hash_without_preceding_space() {
        assert_eq!(
            parse_atx_heading("# C#").map(|(_, title)| title),
            Some("C#".to_string())
        );
        assert_eq!(
            parse_atx_heading("# Title ###").map(|(_, title)| title),
            Some("Title".to_string())
        );
    }

    #[test]
    fn normalize_strips_trailing_whitespace_and_collapses_blank_lines() {
        // MD009 (trailing whitespace) + MD012 (multiple blank lines).
        assert_eq!(normalize("alpha   \n\n\n\nbeta\t\n"), "alpha\n\nbeta\n");
    }

    #[test]
    fn normalize_surrounds_headings_with_blank_lines() {
        // MD022.
        assert_eq!(
            normalize("intro\n# Heading\nbody\n"),
            "intro\n\n# Heading\n\nbody\n"
        );
    }

    #[test]
    fn normalize_does_not_insert_blank_before_leading_heading() {
        // MD022 does not require a blank line above the first line of a file.
        assert_eq!(normalize("# Title\ntext\n"), "# Title\n\ntext\n");
    }

    #[test]
    fn normalize_surrounds_fenced_code_with_blank_lines() {
        // MD031.
        assert_eq!(
            normalize("before\n```rust\nlet x = 1;\n```\nafter\n"),
            "before\n\n```rust\nlet x = 1;\n```\n\nafter\n"
        );
    }

    #[test]
    fn normalize_preserves_fenced_code_content_verbatim() {
        // Trailing whitespace and blank runs inside a fence are not touched.
        let input = "```\ncode  trailing   \n\n\nblank\n```\n";
        assert_eq!(normalize(input), input);
    }

    #[test]
    fn normalize_ends_with_exactly_one_trailing_newline() {
        // MD047.
        assert_eq!(normalize("text"), "text\n");
        assert_eq!(normalize("text\n\n\n"), "text\n");
    }

    #[test]
    fn normalize_preserves_frontmatter_and_separates_first_heading() {
        assert_eq!(
            normalize("---\ntitle: Page\n---\n# Heading\nbody\n"),
            "---\ntitle: Page\n---\n\n# Heading\n\nbody\n"
        );
    }

    #[test]
    fn normalize_keeps_wikilink_and_table_pipes_intact() {
        let normalized =
            normalize("## Refs\n| Name | Link |\n| --- | --- |\n| A | [[Page|Alias]] |\n");
        assert_eq!(
            normalized,
            "## Refs\n\n| Name | Link |\n| --- | --- |\n| A | [[Page|Alias]] |\n"
        );
    }

    #[test]
    fn normalize_is_idempotent() {
        let input = "---\ntitle: T\n---\n\n\n#  Heading  \ntext   \n```\ncode \n```\nmore\n";
        let once = normalize(input);
        assert_eq!(normalize(&once), once);
    }
}
