use std::fs;
use std::path::Path;

use crate::WikiError;
use crate::support::text::slugify_with_options;

use super::types::SourceRecord;
use super::{
    GENERATED_SOURCE_MANIFEST_END, GENERATED_SOURCE_MANIFEST_START, SOURCE_ID_HASH_PREFIX_LEN,
    SOURCE_MARKER,
};

const EMPTY_CONTENT_HASH_SENTINEL: &str = "0000000000000000";

pub(crate) fn render_entry(entry: &SourceRecord, index: &mut String) -> Result<(), WikiError> {
    let metadata = serde_json::to_string(entry).map_err(|error| WikiError::Json {
        action: "serialize raw source index marker",
        path: None,
        source: error,
    })?;
    let title = escape_markdown_text(entry.title.as_deref().unwrap_or(&entry.location));
    let destination = escape_markdown_destination(&entry.location);
    let citation = entry
        .citation
        .as_ref()
        .map(|citation| format!("  - citation: {}\n", inline_text(citation)))
        .unwrap_or_default();
    let license = entry
        .license
        .as_ref()
        .map(|license| format!("  - license: {}\n", inline_text(license)))
        .unwrap_or_default();

    index.push_str(&format!(
        "- [{title}]({destination})\n  - id: `{}`\n  - canonical: `{}`\n  - kind: `{}`\n  - fetched_at: `{}`\n  - hash: `{}`\n{citation}{license}  - ingestion_method: `{}`\n  - compile_status: `{}`\n  - {SOURCE_MARKER}{metadata} -->\n",
        entry.id,
        entry.canonical_location,
        entry.kind,
        entry.fetched_at,
        entry.content_hash,
        entry.ingestion_method,
        entry.compile_status,
    ));
    Ok(())
}

pub(crate) fn canonicalize_location(location: &str) -> String {
    let without_fragment = location.trim().split('#').next().unwrap_or("").trim();
    let canonical = lower_url_scheme_and_authority(without_fragment);
    let (mut base, query) = split_sorted_query(&canonical);
    while base.ends_with('/') && base != "/" && !base.ends_with("://") {
        base.pop();
    }
    match query {
        Some(query) if !query.is_empty() => format!("{base}?{query}"),
        _ => base,
    }
}

fn split_sorted_query(location: &str) -> (String, Option<String>) {
    let Some((base, query)) = location.split_once('?') else {
        return (location.to_string(), None);
    };
    let mut params = query
        .split('&')
        .filter(|param| !param.is_empty())
        .collect::<Vec<_>>();
    params.sort_unstable();
    (base.to_string(), Some(params.join("&")))
}

pub(crate) struct PreservedSourceIndex {
    pub(crate) prefix: String,
    pub(crate) suffix: String,
}

pub(crate) fn existing_index_without_manifest(
    index_path: &Path,
) -> Result<PreservedSourceIndex, WikiError> {
    if !index_path.exists() {
        return Ok(PreservedSourceIndex {
            prefix: String::from("# Raw Sources\n\n"),
            suffix: String::new(),
        });
    }

    let existing = fs::read_to_string(index_path).map_err(|error| WikiError::Io {
        action: "read raw source index",
        path: Some(index_path.to_path_buf()),
        source: error,
    })?;

    if let Some(start) = existing.find(GENERATED_SOURCE_MANIFEST_START) {
        let search_from = start + GENERATED_SOURCE_MANIFEST_START.len();
        if let Some(relative_end) = existing[search_from..].find(GENERATED_SOURCE_MANIFEST_END) {
            let end = search_from + relative_end + GENERATED_SOURCE_MANIFEST_END.len();
            return Ok(PreservedSourceIndex {
                prefix: normalize_preserved_index_prefix(&existing[..start]),
                suffix: normalize_preserved_index_suffix(&existing[end..]),
            });
        }
    }

    if let Some(start) = existing.find("\n## Source manifest") {
        let header_start = start + 1;
        return Ok(PreservedSourceIndex {
            prefix: normalize_preserved_index_prefix(&existing[..start]),
            suffix: suffix_after_unmarked_manifest(&existing, header_start),
        });
    }
    if let Some(header_start) = existing.find("## Source manifest")
        && existing[..header_start].trim().is_empty()
    {
        return Ok(PreservedSourceIndex {
            prefix: String::from("# Raw Sources\n\n"),
            suffix: suffix_after_unmarked_manifest(&existing, header_start),
        });
    }

    Ok(PreservedSourceIndex {
        prefix: normalize_preserved_index_prefix(&existing),
        suffix: String::new(),
    })
}

fn normalize_preserved_index_prefix(prefix: &str) -> String {
    let mut prefix = prefix.trim_end_matches('\n').to_string();
    if prefix.trim().is_empty() {
        prefix.push_str("# Raw Sources");
    }
    prefix.push_str("\n\n");
    prefix
}

fn normalize_preserved_index_suffix(suffix: &str) -> String {
    suffix.trim_start_matches('\n').to_string()
}

fn suffix_after_unmarked_manifest(existing: &str, header_start: usize) -> String {
    let after_header = header_start + "## Source manifest".len();
    existing[after_header..]
        .find("\n## ")
        .map(|offset| normalize_preserved_index_suffix(&existing[after_header + offset + 1..]))
        .unwrap_or_default()
}

fn lower_url_scheme_and_authority(location: &str) -> String {
    let Some((scheme, rest)) = location.split_once("://") else {
        return location.replace('\\', "/");
    };
    let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
    if path.is_empty() {
        format!(
            "{}://{}",
            scheme.to_ascii_lowercase(),
            authority.to_ascii_lowercase()
        )
    } else {
        format!(
            "{}://{}/{}",
            scheme.to_ascii_lowercase(),
            authority.to_ascii_lowercase(),
            path
        )
    }
}

pub(crate) fn source_id(canonical_location: &str, content_hash: &str) -> String {
    // Sixteen hex chars gives a 64-bit collision space while keeping source IDs
    // readable in Markdown manifests.
    let hash_prefix = if content_hash.is_empty() {
        EMPTY_CONTENT_HASH_SENTINEL
    } else {
        &content_hash[..content_hash.len().min(SOURCE_ID_HASH_PREFIX_LEN)]
    };
    let slug = slugify_with_options(canonical_location, None, Some(48));

    if slug.is_empty() {
        format!("src-{hash_prefix}")
    } else {
        format!("src-{hash_prefix}-{slug}")
    }
}

fn escape_markdown_text(text: &str) -> String {
    normalize_newlines(text)
        .replace('\\', "\\\\")
        .replace('[', "\\[")
        .replace(']', "\\]")
}

fn escape_markdown_destination(destination: &str) -> String {
    normalize_newlines(destination)
        .replace('\\', "\\\\")
        .replace('(', "\\(")
        .replace(')', "\\)")
}

fn inline_text(text: &str) -> String {
    normalize_newlines(text)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn normalize_newlines(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markdown_link_escaping_escapes_backslashes_before_breakers() {
        assert_eq!(escape_markdown_text(r"a\[b]"), r"a\\\[b\]");
        assert_eq!(
            escape_markdown_destination(r"https://example.test/a\(b)"),
            r"https://example.test/a\\\(b\)"
        );
    }

    #[test]
    fn inline_text_normalizes_newlines_and_collapses_whitespace() {
        assert_eq!(
            inline_text("alpha\r\n  beta\tgamma\rdone"),
            "alpha beta gamma done"
        );
    }

    #[test]
    fn source_id_uses_empty_hash_sentinel() {
        assert_eq!(source_id("", ""), "src-0000000000000000");
    }
}
