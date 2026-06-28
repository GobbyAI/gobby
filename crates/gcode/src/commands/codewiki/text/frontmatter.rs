use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use crate::commands::codewiki::{CodewikiAiOutcome, GRAPH_UNAVAILABLE, SourceSpan, VerifyNote};

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
    ai_route: &'static str,
    ai_fallback: bool,
    ai_generation_status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    degraded: Option<bool>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    degraded_sources: Vec<&'a str>,
    /// Generation lane for an aggregate page produced by the Lane B tool loop
    /// (#978): `tool_loop`. Absent for Lane A / leaf pages.
    #[serde(skip_serializing_if = "Option::is_none")]
    lane: Option<&'a str>,
    /// Lane B tool-loop call count, recorded alongside `lane`.
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_call_count: Option<usize>,
    /// Lane B completion turns, recorded alongside `lane`.
    #[serde(skip_serializing_if = "Option::is_none")]
    turns: Option<usize>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    verify_notes: Vec<FrontmatterVerifyNote<'a>>,
}

/// Lane B observability recorded into an aggregate page's frontmatter (#978):
/// the generation lane and the tool loop's call/turn counts.
pub(crate) struct FrontmatterLaneB<'a> {
    pub(crate) lane: &'a str,
    pub(crate) tool_call_count: usize,
    pub(crate) turns: usize,
}

#[derive(Clone, serde::Serialize)]
struct FrontmatterSourceFile<'a> {
    file: &'a str,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    ranges: Vec<String>,
}

#[derive(serde::Serialize)]
struct FrontmatterVerifyNote<'a> {
    id: usize,
    reason: &'a str,
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
    frontmatter_with_options(title, kind, source_spans, degraded_sources, true, &[], None)
}

pub(crate) fn frontmatter_with_degradation_without_ranges(
    title: &str,
    kind: &str,
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
) -> String {
    frontmatter_with_options(
        title,
        kind,
        source_spans,
        degraded_sources,
        false,
        &[],
        None,
    )
}

pub(crate) fn frontmatter_with_degradation_and_verify_notes_without_ranges(
    title: &str,
    kind: &str,
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
    verify_notes: &[VerifyNote],
) -> String {
    frontmatter_with_options(
        title,
        kind,
        source_spans,
        degraded_sources,
        false,
        verify_notes,
        None,
    )
}

/// Aggregate-page frontmatter (no symbol line ranges) carrying optional Lane B
/// observability (#978). `lane_b` is `Some` only for a page produced by the
/// tool loop; a Lane A / structural aggregate passes `None`.
pub(crate) fn frontmatter_aggregate_without_ranges(
    title: &str,
    kind: &str,
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
    lane_b: Option<FrontmatterLaneB<'_>>,
) -> String {
    frontmatter_with_options(
        title,
        kind,
        source_spans,
        degraded_sources,
        false,
        &[],
        lane_b,
    )
}

/// Aggregate-page frontmatter with verifier notes (curated concept/narrative
/// pages) carrying optional Lane B observability (#978).
pub(crate) fn frontmatter_aggregate_with_verify_notes(
    title: &str,
    kind: &str,
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
    verify_notes: &[VerifyNote],
    lane_b: Option<FrontmatterLaneB<'_>>,
) -> String {
    frontmatter_with_options(
        title,
        kind,
        source_spans,
        degraded_sources,
        false,
        verify_notes,
        lane_b,
    )
}

fn frontmatter_with_options(
    title: &str,
    kind: &str,
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
    include_ranges: bool,
    verify_notes: &[VerifyNote],
    lane_b: Option<FrontmatterLaneB<'_>>,
) -> String {
    let (source_files, provenance_truncated) =
        frontmatter_source_files(source_spans, include_ranges);
    let ai_outcome = CodewikiAiOutcome::default();
    // `graph-unavailable` is data-source (evidence) degradation, not a page
    // failure: it is listed in `degraded_sources` for transparency but never
    // flips `degraded: true` (#978). Every other code (model-*, grounding-empty)
    // marks a structural fallback and does flip the flag.
    let page_degraded = degraded_sources
        .iter()
        .any(|code| code != GRAPH_UNAVAILABLE);
    let data = Frontmatter {
        title,
        kind,
        provenance: source_files,
        provenance_truncated,
        generated_by: gobby_core::codewiki_contract::GENERATED_BY_CODEWIKI,
        trust: gobby_core::codewiki_contract::TRUST_GENERATED,
        freshness: gobby_core::codewiki_contract::FRESHNESS_INDEXED,
        ai_route: ai_outcome.route_label(),
        ai_fallback: ai_outcome.fallback,
        ai_generation_status: ai_outcome.status.as_str(),
        degraded: page_degraded.then_some(true),
        degraded_sources: degraded_sources.iter().map(String::as_str).collect(),
        lane: lane_b.as_ref().map(|lane_b| lane_b.lane),
        tool_call_count: lane_b.as_ref().map(|lane_b| lane_b.tool_call_count),
        turns: lane_b.as_ref().map(|lane_b| lane_b.turns),
        verify_notes: verify_notes
            .iter()
            .map(|note| FrontmatterVerifyNote {
                id: note.id,
                reason: note.reason.as_str(),
            })
            .collect(),
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

pub(crate) fn append_relevant_source_files(doc: &mut String, source_spans: &[SourceSpan]) {
    let (source_files, provenance_truncated) = frontmatter_source_files(source_spans, true);
    if source_files.is_empty() {
        return;
    }

    doc.push_str("<details>\n<summary>Relevant source files</summary>\n\n");
    for source in source_files {
        doc.push_str("- ");
        if source.ranges.is_empty() {
            let _ = write!(
                doc,
                "[{}]({})",
                escape_markdown_link_label(source.file),
                encode_markdown_path(source.file)
            );
        } else {
            for (index, range) in source.ranges.iter().enumerate() {
                if index > 0 {
                    doc.push_str(", ");
                }
                let _ = write!(
                    doc,
                    "[{}:{}]({})",
                    escape_markdown_link_label(source.file),
                    escape_markdown_link_label(range),
                    source_range_href(source.file, range)
                );
            }
        }
        doc.push('\n');
    }
    if let Some(count) = provenance_truncated {
        let noun = if count == 1 { "file" } else { "files" };
        let _ = writeln!(doc, "\n_{count} more source {noun} omitted._");
    }
    doc.push_str("\n</details>\n\n");
}

/// Bounded "Relevant source files" block for curated pages: no per-range
/// expansion and capped at `limit` files. Reference pages keep the full,
/// range-complete [`append_relevant_source_files`] block (the agent-grounding
/// surface); curated pages keep a small provenance footprint so prose, not
/// provenance, dominates the file (#853 root cause 5).
pub(crate) fn append_curated_source_files(
    doc: &mut String,
    source_spans: &[SourceSpan],
    limit: usize,
) {
    let (mut source_files, provenance_truncated) = frontmatter_source_files(source_spans, false);
    let mut omitted = provenance_truncated.unwrap_or(0);
    if source_files.len() > limit {
        omitted += source_files.len() - limit;
        source_files.truncate(limit);
    }
    if source_files.is_empty() {
        return;
    }
    doc.push_str("<details>\n<summary>Relevant source files</summary>\n\n");
    for source in source_files {
        let _ = writeln!(
            doc,
            "- [{}]({})",
            escape_markdown_link_label(source.file),
            encode_markdown_path(source.file)
        );
    }
    if omitted > 0 {
        let noun = if omitted == 1 { "file" } else { "files" };
        let _ = writeln!(doc, "\n_{omitted} more source {noun} omitted._");
    }
    doc.push_str("\n</details>\n\n");
}

fn frontmatter_source_files(
    source_spans: &[SourceSpan],
    include_ranges: bool,
) -> (Vec<FrontmatterSourceFile<'_>>, Option<usize>) {
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
    (
        source_files,
        (provenance_truncated > 0).then_some(provenance_truncated),
    )
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

fn source_range_href(file: &str, range: &str) -> String {
    let anchor = match range.split_once('-') {
        Some((start, end)) => format!("#L{start}-L{end}"),
        None => format!("#L{range}"),
    };
    format!("{}{}", encode_markdown_path(file), anchor)
}

fn encode_markdown_path(path: &str) -> String {
    let mut encoded = String::new();
    for (index, segment) in path.split('/').enumerate() {
        if index > 0 {
            encoded.push('/');
        }
        encoded.push_str(&urlencoding::encode(segment));
    }
    encoded
}

fn escape_markdown_link_label(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('[', "\\[")
        .replace(']', "\\]")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spans() -> Vec<SourceSpan> {
        vec![SourceSpan {
            file: "src/lib.rs".to_string(),
            line_start: 1,
            line_end: 4,
        }]
    }

    #[test]
    fn verify_notes_are_omitted_when_empty() {
        let doc = frontmatter_with_degradation_and_verify_notes_without_ranges(
            "src/lib.rs",
            "code_file",
            &spans(),
            &[],
            &[],
        );

        assert!(!doc.contains("verify_notes:"));
    }

    #[test]
    fn verify_notes_render_as_yaml_list() {
        let notes = vec![VerifyNote::new(2, " unsupported claim ")];
        let doc = frontmatter_with_degradation_and_verify_notes_without_ranges(
            "src/lib.rs",
            "code_file",
            &spans(),
            &[],
            &notes,
        );
        let yaml = doc
            .strip_prefix("---\n")
            .and_then(|doc| doc.split_once("---\n\n").map(|(yaml, _)| yaml))
            .expect("frontmatter delimiters");
        let frontmatter: serde_yaml::Value =
            serde_yaml::from_str(yaml).expect("frontmatter parses");
        let notes = frontmatter["verify_notes"]
            .as_sequence()
            .expect("verify_notes is a list");

        assert_eq!(notes.len(), 1);
        assert_eq!(notes[0]["id"].as_i64(), Some(2));
        assert_eq!(notes[0]["reason"].as_str(), Some("unsupported claim"));
    }

    #[test]
    fn aggregate_frontmatter_records_lane_b_observability() {
        let with_lane_b = frontmatter_aggregate_without_ranges(
            "Repository Overview",
            "code_repo",
            &spans(),
            &[],
            Some(FrontmatterLaneB {
                lane: "tool_loop",
                tool_call_count: 5,
                turns: 3,
            }),
        );
        assert!(with_lane_b.contains("lane: tool_loop"), "{with_lane_b}");
        assert!(with_lane_b.contains("tool_call_count: 5"), "{with_lane_b}");
        assert!(with_lane_b.contains("turns: 3"), "{with_lane_b}");

        // A Lane A / structural aggregate records no lane observability.
        let without = frontmatter_aggregate_without_ranges(
            "Repository Overview",
            "code_repo",
            &spans(),
            &[],
            None,
        );
        assert!(!without.contains("lane:"), "{without}");
        assert!(!without.contains("tool_call_count:"), "{without}");
        assert!(!without.contains("turns:"), "{without}");
    }

    #[test]
    fn graph_unavailable_is_listed_but_does_not_mark_the_page_degraded() {
        // Evidence degradation: graph-unavailable is recorded for transparency
        // but never flips `degraded: true` (#978).
        let evidence = frontmatter_with_degradation(
            "Repository Overview",
            "code_repo",
            &spans(),
            &[GRAPH_UNAVAILABLE.to_string()],
        );
        assert!(evidence.contains("graph-unavailable"), "{evidence}");
        assert!(!evidence.contains("degraded: true"), "{evidence}");

        // A genuine AI-generation failure does mark the page degraded.
        let failure = frontmatter_with_degradation(
            "Repository Overview",
            "code_repo",
            &spans(),
            &["model-unavailable".to_string()],
        );
        assert!(failure.contains("degraded: true"), "{failure}");
        assert!(failure.contains("model-unavailable"), "{failure}");
    }
}
