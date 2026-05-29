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
    let mut fence: Option<&str> = None;

    while offset < markdown.len() {
        let line_end = markdown[offset..]
            .find('\n')
            .map_or(markdown.len(), |relative| offset + relative);
        let line_content_end = markdown[..line_end]
            .strip_suffix('\r')
            .map_or(line_end, |line| line.len());
        let line = &markdown[offset..line_content_end];
        let trimmed = line.trim_start();

        if let Some(active_fence) = fence {
            if trimmed.starts_with(active_fence) {
                fence = None;
            }
        } else if trimmed.starts_with("```") {
            fence = Some("```");
        } else if trimmed.starts_with("~~~") {
            fence = Some("~~~");
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

fn parse_atx_heading(line: &str) -> Option<(u8, String)> {
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

    let title = after_marks
        .trim()
        .trim_end_matches('#')
        .trim_end()
        .to_string();
    Some((level as u8, title))
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

        let parsed =
            parse_markdown("wiki/topics/parser.md", markdown, ["Known"]).expect("parse markdown");

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
    fn index_parse_is_read_only() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let page = tmp.path().join("wiki/topics/Page.md");
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
}
