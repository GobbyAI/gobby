use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use gobby_core::markdown::{
    frontmatter_body_start, normalize_markdown, yaml_frontmatter_closing_delimiter_start,
};

use crate::WikiError;

/// Canonical Markdown body used for generated-page identity.
///
/// Frontmatter is intentionally excluded so operational metadata changes do
/// not invalidate citations. The shared Markdown normalizer makes equivalent
/// generated whitespace produce the same identity.
pub(crate) fn normalized_body(markdown: &str) -> String {
    let body_start = frontmatter_body_start(markdown).unwrap_or(0);
    normalize_markdown(&markdown[body_start..])
}

/// Lowercase SHA-256 identity of a page's canonical Markdown body.
pub(crate) fn content_hash(markdown: &str) -> String {
    gobby_core::indexing::content_hash(normalized_body(markdown).as_bytes())
}

/// Recompute a page identity from Markdown currently stored on disk.
pub(crate) fn content_hash_from_path(path: &Path) -> Result<String, WikiError> {
    let markdown = fs::read_to_string(path).map_err(|source| WikiError::Io {
        action: "read wiki page for content hash",
        path: Some(path.to_path_buf()),
        source,
    })?;
    Ok(content_hash(&markdown))
}

/// Stamp a newly generated YAML-frontmatter page with its current body hash
/// and the sorted, deduplicated hashes of all contributing sources.
pub(crate) fn stamp_generated_page<'a>(
    markdown: &str,
    source_hashes: impl IntoIterator<Item = &'a str>,
) -> String {
    let page_hash = content_hash(markdown);
    let compiled_from: BTreeSet<&str> = source_hashes.into_iter().collect();
    let mut metadata = format!("content_hash: {page_hash}\n");
    if compiled_from.is_empty() {
        metadata.push_str("compiled_from: []\n");
    } else {
        metadata.push_str("compiled_from:\n");
        for source_hash in compiled_from {
            metadata.push_str("  - ");
            metadata.push_str(source_hash);
            metadata.push('\n');
        }
    }

    let Some(closing_start) = yaml_frontmatter_closing_delimiter_start(markdown) else {
        return format!("---\n{metadata}---\n\n{markdown}");
    };
    let mut stamped = String::with_capacity(markdown.len() + metadata.len());
    stamped.push_str(&markdown[..closing_start]);
    stamped.push_str(&metadata);
    stamped.push_str(&markdown[closing_start..]);
    stamped
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn body_hash_ignores_frontmatter_and_normalizes_markdown() {
        let first = "---\nlifecycle: draft\ncompile_handoff: one\naliases:\n  - Example\n---\n\n# Example  \n\n\nBody.\n";
        let second = "---\nlifecycle: verified\ncompile_handoff: two\naliases: []\n---\n# Example\n\nBody.\n\n";
        let changed = "---\nlifecycle: verified\n---\n# Example\n\nChanged body.\n";

        assert_eq!(content_hash(first), content_hash(second));
        assert_ne!(content_hash(first), content_hash(changed));
    }

    #[test]
    fn generated_page_stamp_is_canonical() {
        let markdown = "---\ntitle: Example\n---\n\n# Example\n\nBody.\n";
        let stamped = stamp_generated_page(markdown, ["bbb", "aaa", "bbb"]);

        assert!(stamped.contains(&format!("content_hash: {}\n", content_hash(markdown))));
        assert!(stamped.contains("compiled_from:\n  - aaa\n  - bbb\n"));
        assert_eq!(content_hash(&stamped), content_hash(markdown));
    }

    #[test]
    fn generated_page_without_frontmatter_receives_version_frontmatter() {
        let markdown = "# Example\n\nBody.\n";
        let stamped = stamp_generated_page(markdown, std::iter::empty());

        assert!(stamped.starts_with("---\ncontent_hash: "));
        assert!(stamped.contains("\ncompiled_from: []\n---\n\n# Example"));
        assert_eq!(content_hash(&stamped), content_hash(markdown));
    }

    #[test]
    fn generated_page_stamp_reuses_crlf_frontmatter_with_spaced_delimiters() {
        let markdown = "  ---  \r\ntitle: Example\r\n  ---  \r\n\r\n# Example\r\n";

        let stamped = stamp_generated_page(markdown, []);

        assert!(stamped.starts_with("  ---  \r\ntitle: Example\r\ncontent_hash: "));
        assert!(stamped.contains("compiled_from: []\n  ---  \r\n\r\n# Example\r\n"));
        assert_eq!(stamped.matches("title: Example").count(), 1);
    }
}
