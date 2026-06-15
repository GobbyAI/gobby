use std::collections::{BTreeMap, BTreeSet};

use crate::commands::codewiki::SourceSpan;

#[derive(serde::Serialize)]
struct Frontmatter<'a> {
    title: &'a str,
    #[serde(rename = "type")]
    kind: &'a str,
    provenance: Vec<FrontmatterSourceFile<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    provenance_truncated: Option<usize>,
    generated_by: &'static str,
    trust: &'static str,
    freshness: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    degraded: Option<bool>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    degraded_sources: Vec<&'a str>,
}

#[derive(Clone, serde::Serialize)]
struct FrontmatterSourceFile<'a> {
    file: &'a str,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    ranges: Vec<String>,
}

/// Cap on the number of provenance files listed in page frontmatter; pages
/// rolling up more files keep the top contributors by span count and record
/// the omitted count as `provenance_truncated`.
pub(super) const MAX_FRONTMATTER_PROVENANCE_FILES: usize = 30;

#[cfg(test)]
pub(crate) fn frontmatter(title: &str, kind: &str, source_spans: &[SourceSpan]) -> String {
    frontmatter_with_degradation(title, kind, source_spans, &[])
}

/// Builds the same generated frontmatter as `frontmatter`, plus optional
/// `degraded` and `degraded_sources` fields when graph/AI inputs are partial.
pub(crate) fn frontmatter_with_degradation(
    title: &str,
    kind: &str,
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
) -> String {
    frontmatter_with_options(title, kind, source_spans, degraded_sources, true)
}

pub(crate) fn frontmatter_with_degradation_without_ranges(
    title: &str,
    kind: &str,
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
) -> String {
    frontmatter_with_options(title, kind, source_spans, degraded_sources, false)
}

fn frontmatter_with_options(
    title: &str,
    kind: &str,
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
    include_ranges: bool,
) -> String {
    let mut files: BTreeMap<&str, BTreeSet<(usize, usize)>> = BTreeMap::new();
    for span in source_spans {
        files
            .entry(&span.file)
            .or_default()
            .insert((span.line_start, span.line_end));
    }

    // Aggregate pages can roll up provenance for hundreds of files; cap the
    // frontmatter at the files contributing the most spans and record how
    // many were omitted so the truncation is visible, not silent.
    let provenance_truncated = files.len().saturating_sub(MAX_FRONTMATTER_PROVENANCE_FILES);
    if provenance_truncated > 0 {
        let mut ranked = files.keys().copied().collect::<Vec<_>>();
        ranked.sort_by_key(|file| {
            (
                std::cmp::Reverse(files.get(file).map_or(0, BTreeSet::len)),
                *file,
            )
        });
        let kept = ranked
            .into_iter()
            .take(MAX_FRONTMATTER_PROVENANCE_FILES)
            .collect::<BTreeSet<_>>();
        files.retain(|file, _| kept.contains(file));
    }

    let source_files: Vec<FrontmatterSourceFile<'_>> = files
        .into_iter()
        .map(|(file, ranges)| FrontmatterSourceFile {
            file,
            ranges: if include_ranges {
                format_frontmatter_ranges(ranges)
            } else {
                Vec::new()
            },
        })
        .collect();
    let data = Frontmatter {
        title,
        kind,
        provenance: source_files,
        provenance_truncated: (provenance_truncated > 0).then_some(provenance_truncated),
        generated_by: gobby_core::codewiki_contract::GENERATED_BY_CODEWIKI,
        trust: gobby_core::codewiki_contract::TRUST_GENERATED,
        freshness: gobby_core::codewiki_contract::FRESHNESS_INDEXED,
        degraded: (!degraded_sources.is_empty()).then_some(true),
        degraded_sources: degraded_sources.iter().map(String::as_str).collect(),
    };
    let yaml = serde_yaml::to_string(&data)
        .expect("codewiki frontmatter only contains YAML-serializable data");
    let yaml = yaml.strip_prefix("---\n").unwrap_or(&yaml);

    let mut out = String::from("---\n");
    out.push_str(yaml);
    if !out.ends_with('\n') {
        out.push('\n');
    }
    out.push_str("---\n\n");
    out
}

fn format_frontmatter_ranges(ranges: BTreeSet<(usize, usize)>) -> Vec<String> {
    let mut merged: Vec<(usize, usize)> = Vec::new();
    for (line_start, line_end) in ranges {
        let start = line_start.min(line_end);
        let end = line_start.max(line_end);
        if let Some((_, previous_end)) = merged.last_mut()
            && start <= previous_end.saturating_add(1)
        {
            *previous_end = (*previous_end).max(end);
            continue;
        }
        merged.push((start, end));
    }

    merged
        .into_iter()
        .map(|(line_start, line_end)| {
            if line_start == line_end {
                line_start.to_string()
            } else {
                format!("{line_start}-{line_end}")
            }
        })
        .collect()
}
