use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use aho_corasick::AhoCorasick;
use gobby_core::vault::links::{LinkKind, canonical_target_key, normalize_wiki_path};
use gobby_core::vault::lint::link_lookup_keys;

use crate::lint::WikiPage;
use crate::markdown::{MarkdownFence, markdown_fence_closes, markdown_fence_start};
use crate::provenance::ProvenanceGraph;
use crate::sources::SourceRecord;

#[derive(Default)]
pub(super) struct SourceCitationIndex {
    cited_source_ids: BTreeSet<String>,
}

impl SourceCitationIndex {
    pub(super) fn cites(&self, source_id: &str) -> bool {
        self.cited_source_ids.contains(source_id)
    }
}

pub(crate) struct SourceNeedleIndex {
    text_patterns: Vec<String>,
    text_source_ids: Vec<BTreeSet<String>>,
    link_source_ids_by_target: BTreeMap<String, BTreeSet<String>>,
}

pub(super) fn build_citation_index(
    sources: &[SourceRecord],
    pages: &[WikiPage],
    provenance: &ProvenanceGraph,
    needle_index: &SourceNeedleIndex,
) -> SourceCitationIndex {
    let mut cited_source_ids = sources
        .iter()
        .filter(|source| !provenance.links_for_source(&source.id).is_empty())
        .map(|source| source.id.clone())
        .collect::<BTreeSet<_>>();
    for page in pages {
        cited_source_ids.extend(page_cited_source_ids(page, needle_index));
    }
    if needle_index.text_patterns.is_empty() {
        return SourceCitationIndex { cited_source_ids };
    }
    let matcher = match AhoCorasick::new(&needle_index.text_patterns) {
        Ok(matcher) => matcher,
        Err(error) => {
            log::warn!("failed to build health citation matcher: {error}");
            return SourceCitationIndex { cited_source_ids };
        }
    };

    for page in pages {
        let markdown = markdown_without_fenced_code(&page.markdown);
        for matched in matcher.find_overlapping_iter(&markdown) {
            if !has_text_match_boundaries(&markdown, matched.start(), matched.end()) {
                continue;
            }
            for source_id in &needle_index.text_source_ids[matched.pattern().as_usize()] {
                cited_source_ids.insert(source_id.clone());
            }
        }
    }
    SourceCitationIndex { cited_source_ids }
}

pub(crate) fn build_source_needle_index(sources: &[SourceRecord]) -> SourceNeedleIndex {
    let mut text_source_ids_by_needle = BTreeMap::<String, BTreeSet<String>>::new();
    let mut link_source_ids_by_target = BTreeMap::<String, BTreeSet<String>>::new();
    for source in sources {
        for needle in source_reference_needles(source) {
            insert_source_needle(
                &mut text_source_ids_by_needle,
                &mut link_source_ids_by_target,
                source,
                needle,
            );
        }
        insert_link_target(
            &mut link_source_ids_by_target,
            &format!("knowledge/sources/{}", source.id),
            &source.id,
        );
        insert_link_target(
            &mut link_source_ids_by_target,
            &format!("knowledge/sources/{}.md", source.id),
            &source.id,
        );
    }
    let (text_patterns, text_source_ids): (Vec<_>, Vec<_>) =
        text_source_ids_by_needle.into_iter().unzip();
    SourceNeedleIndex {
        text_patterns,
        text_source_ids,
        link_source_ids_by_target,
    }
}

fn insert_source_needle(
    text_source_ids_by_needle: &mut BTreeMap<String, BTreeSet<String>>,
    link_source_ids_by_target: &mut BTreeMap<String, BTreeSet<String>>,
    source: &SourceRecord,
    needle: &str,
) {
    let needle = needle.trim();
    if needle.is_empty() {
        return;
    }
    text_source_ids_by_needle
        .entry(needle.to_string())
        .or_default()
        .insert(source.id.clone());
    insert_link_target(link_source_ids_by_target, needle, &source.id);
}

fn insert_link_target(
    link_source_ids_by_target: &mut BTreeMap<String, BTreeSet<String>>,
    target: &str,
    source_id: &str,
) {
    let target = target.trim();
    if target.is_empty() {
        return;
    }
    insert_link_key(link_source_ids_by_target, target, source_id);
    let normalized = normalize_wiki_path(target);
    insert_link_key(link_source_ids_by_target, &normalized, source_id);
    insert_link_key(
        link_source_ids_by_target,
        &canonical_target_key(&normalized),
        source_id,
    );
    for kind in [LinkKind::Wikilink, LinkKind::Markdown] {
        for key in link_lookup_keys(Path::new(""), kind, &normalized) {
            insert_link_key(link_source_ids_by_target, &key, source_id);
        }
    }
}

fn insert_link_key(
    link_source_ids_by_target: &mut BTreeMap<String, BTreeSet<String>>,
    key: &str,
    source_id: &str,
) {
    link_source_ids_by_target
        .entry(key.to_string())
        .or_default()
        .insert(source_id.to_string());
}

/// Registered source ids this page's links resolve to. Shared by the vault
/// citation index and per-page confidence composition.
pub(super) fn page_cited_source_ids(
    page: &WikiPage,
    needle_index: &SourceNeedleIndex,
) -> BTreeSet<String> {
    let mut cited_source_ids = BTreeSet::new();
    for link in &page.parsed.links {
        cite_link_key(&mut cited_source_ids, needle_index, &link.target);
        cite_link_key(&mut cited_source_ids, needle_index, &link.normalized_target);
        for key in link_lookup_keys(&page.relative_path, link.kind, &link.normalized_target) {
            cite_link_key(&mut cited_source_ids, needle_index, &key);
        }
    }
    cited_source_ids
}

fn cite_link_key(
    cited_source_ids: &mut BTreeSet<String>,
    needle_index: &SourceNeedleIndex,
    key: &str,
) {
    if let Some(source_ids) = needle_index.link_source_ids_by_target.get(key) {
        cited_source_ids.extend(source_ids.iter().cloned());
    }
}

fn source_reference_needles(source: &SourceRecord) -> Vec<&str> {
    let mut needles = vec![
        source.id.as_str(),
        source.location.as_str(),
        source.canonical_location.as_str(),
    ];
    if let Some(citation) = source.citation.as_deref() {
        needles.push(citation);
    }
    needles
}

pub(super) fn markdown_without_fenced_code(markdown: &str) -> String {
    let mut output = String::new();
    let mut active_fence: Option<MarkdownFence> = None;
    for line in markdown.lines() {
        if let Some(fence) = active_fence {
            if markdown_fence_closes(line, fence) {
                active_fence = None;
                continue;
            }
        } else if let Some(fence) = markdown_fence_start(line) {
            active_fence = Some(fence);
            continue;
        }
        if active_fence.is_none() {
            output.push_str(line);
            output.push('\n');
        }
    }
    output
}

pub(super) fn has_text_match_boundaries(markdown: &str, start: usize, end: usize) -> bool {
    let before = markdown[..start].chars().next_back();
    let after = markdown[end..].chars().next();
    !before.is_some_and(is_citation_word_char) && !after.is_some_and(is_citation_word_char)
}

fn is_citation_word_char(value: char) -> bool {
    value == '_' || value.is_alphanumeric()
}
