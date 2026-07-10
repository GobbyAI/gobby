use std::collections::BTreeSet;
use std::sync::Arc;

use serde_json::Value;

use crate::lint::{WikiPage, line_number};
use crate::markdown::{
    MarkdownFence, markdown_fence_closes, markdown_fence_start, parse_atx_heading,
};
use crate::models::WikiSourceKind;
use crate::provenance::ProvenanceGraph;
use crate::synthesis::slugify;

use super::{
    AuditOptions, AuditSourceContext, ClaimClassification, ClassifiedClaim, UnsupportedClaim,
};

#[derive(Debug, Default)]
pub(super) struct PageClaimAnalysis {
    pub(super) classified: Vec<ClassifiedClaim>,
    pub(super) unsupported: Vec<UnsupportedClaim>,
}

pub(super) fn analyze_claims(
    page: &WikiPage,
    provenance: &ProvenanceGraph,
    source_context: &Arc<Vec<AuditSourceContext>>,
    manifest_hashes: &BTreeSet<String>,
    options: &AuditOptions,
) -> PageClaimAnalysis {
    if is_manifest_backed_source_digest(page, manifest_hashes)
        || is_catalog_page(page)
        || is_recap_page(page)
        || is_generated_code_projection_page(page)
    {
        return PageClaimAnalysis::default();
    }
    let claims = claim_lines(page, options);
    let supported_lines = supported_claim_lines(page, provenance, &claims);
    let has_page_source_support = has_codewiki_frontmatter_source_spans(page);
    let attributed_sections = daemon_synthesis_attributed_sections(page);
    let claim_source_context = claim_source_context(page, source_context);
    let mut analysis = PageClaimAnalysis::default();
    for claim in claims {
        let in_conflict_gap_section = is_conflict_gap_heading(claim.heading.as_deref());
        let extracted =
            supported_lines.contains(&claim.line) || has_inline_source_support(&claim.text);
        let inferred = (has_page_source_support && claim.kind == ClaimKind::Structural)
            || claim
                .heading
                .as_deref()
                .is_some_and(|heading| attributed_sections.contains(heading));
        let classification = if in_conflict_gap_section || has_conflict_gap_prefix(&claim.text) {
            ClaimClassification::Ambiguous
        } else if extracted {
            ClaimClassification::Extracted
        } else if inferred {
            ClaimClassification::Inferred
        } else {
            ClaimClassification::Ambiguous
        };
        // Conflict/gap sections record known uncertainty; they classify as
        // ambiguous but are not audit failures. A conflict-/gap-prefixed line
        // under a normal heading still needs support to escape the
        // unsupported report.
        if !in_conflict_gap_section && !extracted && !inferred {
            analysis.unsupported.push(UnsupportedClaim {
                path: page.relative_path.clone(),
                line: claim.line,
                heading: claim.heading.clone(),
                claim: claim.text.clone(),
                reason: "claim has no source provenance or inline citation".to_string(),
                source_context: Arc::clone(&claim_source_context),
            });
        }
        analysis.classified.push(ClassifiedClaim {
            path: page.relative_path.clone(),
            line: claim.line,
            heading: claim.heading,
            claim: claim.text,
            classification,
        });
    }
    analysis
}

fn claim_source_context(
    page: &WikiPage,
    source_context: &Arc<Vec<AuditSourceContext>>,
) -> Arc<Vec<AuditSourceContext>> {
    if is_generated_codewiki_page(page) {
        Arc::new(Vec::new())
    } else {
        Arc::clone(source_context)
    }
}

/// An auto-generated daemon-synthesis concept page (`source_kind: concept` +
/// `synthesis_mode: daemon`). Unlike curated `knowledge/**` articles, these are
/// clustered from session sources by the daemon, and attribute a synthesized
/// section's prose to its sources with a section-level `_Source: [[...]]_`
/// line rather than an inline citation on every wrapped prose line.
fn is_daemon_synthesis_concept_page(page: &WikiPage) -> bool {
    page.parsed.frontmatter.source_kind == Some(WikiSourceKind::Concept)
        && page
            .parsed
            .frontmatter
            .unknown
            .get("synthesis_mode")
            .and_then(Value::as_str)
            == Some("daemon")
}

/// Section headings on a daemon-synthesis concept page whose body carries an
/// inline source attribution (e.g. a trailing `_Source: [[knowledge/sources/...]]_`
/// line). The daemon cites such a section at the section level, so the
/// line-granular claim model wrongly flags the section's wrapped prose lines as
/// unsupported. Crediting the whole attributed section — scoped to these
/// auto-generated pages — mirrors the page-level grounding
/// `is_generated_code_projection_page` grants deterministic code projections,
/// while leaving curated multi-source synthesis pages fully per-claim audited.
fn daemon_synthesis_attributed_sections(page: &WikiPage) -> BTreeSet<String> {
    if !is_daemon_synthesis_concept_page(page) {
        return BTreeSet::new();
    }
    let mut attributed = BTreeSet::new();
    let mut current_heading: Option<String> = None;
    for raw_line in page.markdown.lines() {
        let trimmed = raw_line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if let Some(heading) = heading_title(trimmed) {
            current_heading = Some(heading);
            continue;
        }
        if let Some(heading) = current_heading.as_ref()
            && has_inline_source_support(trimmed)
        {
            attributed.insert(heading.clone());
        }
    }
    attributed
}

/// Deterministic catalog surfaces rebuilt by `catalog::regenerate` from
/// on-disk vault state with no LLM involvement. They are navigation
/// artifacts — derived listings and `(none yet)` placeholders, never claims
/// needing provenance — so the audit skips them the same way it skips
/// manifest-backed source digests.
const CATALOG_PAGES: &[&str] = &["_index.md", "knowledge/INDEX.md", "code/INDEX.md"];

fn is_catalog_page(page: &WikiPage) -> bool {
    let page_path = page.relative_path.to_string_lossy().replace('\\', "/");
    CATALOG_PAGES.contains(&page_path.as_str())
        || page
            .relative_path
            .file_name()
            .is_some_and(|name| name == "_context.md")
}

/// Daily recap pages under `recaps/` (marked by `recap_date` frontmatter) are
/// page-level supported: the deterministic session listing is structural and
/// the synthesized overview grounds on the day's listed digests.
fn is_recap_page(page: &WikiPage) -> bool {
    let page_path = page.relative_path.to_string_lossy().replace('\\', "/");
    page_path.starts_with("recaps/") && page.parsed.frontmatter.unknown.contains_key("recap_date")
}

fn is_generated_codewiki_page(page: &WikiPage) -> bool {
    let page_path = page.relative_path.to_string_lossy().replace('\\', "/");
    page_path.starts_with("code/")
        && page.parsed.frontmatter.trust.as_deref()
            == Some(gobby_core::codewiki_contract::TRUST_GENERATED)
}

pub(super) fn has_codewiki_frontmatter_source_spans(page: &WikiPage) -> bool {
    let page_path = page.relative_path.to_string_lossy().replace('\\', "/");
    page_path.starts_with("code/")
        && page
            .parsed
            .frontmatter
            .provenance
            .iter()
            .any(frontmatter_value_has_code_source_span)
}

/// A codewiki-generated `code/**` projection page — a per-file, per-module, or
/// repo-level aggregate view of the code index.
///
/// Every such page is generated deterministically from the code index. Its body
/// mixes structural scaffolding (overview counts, `Symbol:`/`Kind:` rows,
/// module/file wikilinks, per-symbol purpose lines) with, for AI-routed pages,
/// flowing narrative *about the same indexed code*. Two facts make per-claim
/// source spans the wrong bar here:
///
/// 1. The page is a projection of the code index for the code it documents, so
///    every claim already traces to that source. The page's identity as a
///    codewiki projection — not a per-claim line span — is the grounding.
/// 2. The audit extracts one claim per physical line, so flowing narrative
///    paragraphs are inherently mostly "unsupported" no matter how densely the
///    generator cites them (a wrapped sentence carries its span on one line and
///    leaves the continuation lines bare). The line-granular claim model fits
///    structural/list content, not prose.
///
/// So these pages are auto-generated texture grounded by the index, not curated
/// multi-source synthesis, and they are exempt from claim scanning — consistent
/// with the catalog/recap/manifest-digest exemptions and with trust gating only
/// on *curated* broken links. This covers `code/files/**`, `code/modules/**`,
/// and the `code/repo.md` aggregate (which carries no per-file frontmatter
/// provenance because it rolls up the whole tree). Curated knowledge pages
/// (`knowledge/**`) are not `code/**` and remain fully audited.
fn is_generated_code_projection_page(page: &WikiPage) -> bool {
    is_generated_codewiki_page(page)
        && page.parsed.frontmatter.generated_by.as_deref()
            == Some(gobby_core::codewiki_contract::GENERATED_BY_CODEWIKI)
}

/// A digest under `knowledge/sources/` whose frontmatter `source_hash` matches
/// a registered manifest record is page-level supported for every claim kind:
/// the digest has exactly one source, so each claim inherits it structurally.
pub(super) fn is_manifest_backed_source_digest(
    page: &WikiPage,
    manifest_hashes: &BTreeSet<String>,
) -> bool {
    let page_path = page.relative_path.to_string_lossy().replace('\\', "/");
    if !page_path.starts_with("knowledge/sources/") {
        return false;
    }
    page.parsed
        .frontmatter
        .unknown
        .get("source_hash")
        .and_then(Value::as_str)
        .is_some_and(|hash| manifest_hashes.contains(hash))
}

fn frontmatter_value_has_code_source_span(value: &Value) -> bool {
    let Value::Array(sources) = value else {
        return false;
    };
    sources.iter().any(frontmatter_source_has_code_span)
}

fn frontmatter_source_has_code_span(value: &Value) -> bool {
    let Value::Object(source) = value else {
        return false;
    };
    let Some(file) = source
        .get(gobby_core::codewiki_contract::PROVENANCE_FILE_KEY)
        .and_then(Value::as_str)
    else {
        return false;
    };
    let Some(Value::Array(ranges)) =
        source.get(gobby_core::codewiki_contract::PROVENANCE_RANGES_KEY)
    else {
        return false;
    };
    is_code_source_path(file) && ranges.iter().any(frontmatter_range_is_valid)
}

fn frontmatter_range_is_valid(value: &Value) -> bool {
    if let Some(range) = value.as_str() {
        is_line_span(range)
    } else {
        value.as_u64().is_some_and(|line| line > 0)
    }
}

fn supported_claim_lines(
    page: &WikiPage,
    provenance: &ProvenanceGraph,
    claims: &[ClaimLine],
) -> BTreeSet<usize> {
    let page_path = page.relative_path.to_string_lossy().replace('\\', "/");
    let page_title = crate::lint::title_for_page(page);
    let page_slug = slugify(&page_title);
    let section_headings: BTreeSet<String> = provenance
        .links()
        .iter()
        .filter(|link| link.section.page_path.to_string_lossy().replace('\\', "/") == page_path)
        .filter(|link| {
            link.section.section_id == page_slug
                || page
                    .parsed
                    .headings
                    .iter()
                    .any(|heading| heading.title == link.section.heading)
        })
        .map(|link| link.section.heading.clone())
        .collect();

    if section_headings.is_empty() {
        return BTreeSet::new();
    }

    claims
        .iter()
        .filter_map(|claim| {
            claim
                .heading
                .as_ref()
                .is_some_and(|heading| section_headings.contains(heading))
                .then_some(claim.line)
        })
        .collect()
}

#[derive(Debug)]
pub(super) struct ClaimLine {
    pub(super) line: usize,
    pub(super) heading: Option<String>,
    pub(super) text: String,
    kind: ClaimKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ClaimKind {
    Prose,
    Structural,
}

/// Extract auditable prose claims from markdown body lines.
///
/// Frontmatter is skipped, headings update ignored-section state, empty lines
/// and markdown-only structural lines are ignored, and lines with inline
/// source support are treated as already supported.
pub(super) fn claim_lines(page: &WikiPage, options: &AuditOptions) -> Vec<ClaimLine> {
    let mut claims = Vec::new();
    let mut offset = 0;
    let mut frontmatter_marker: Option<&str> = None;
    let mut fence: Option<MarkdownFence> = None;
    let mut current_heading: Option<String> = None;
    let mut in_comment = false;

    for raw_line in page.markdown.split_inclusive('\n') {
        let line = raw_line.trim_end_matches(['\r', '\n']);
        let trimmed = line.trim();
        let line_start = offset;
        offset += raw_line.len();

        if line_start == 0 && (trimmed == "---" || trimmed == "+++") {
            frontmatter_marker = Some(trimmed);
            continue;
        }
        if let Some(marker) = frontmatter_marker {
            if trimmed == marker {
                frontmatter_marker = None;
            }
            continue;
        }
        if let Some(active_fence) = fence {
            if markdown_fence_closes(line, active_fence) {
                fence = None;
            }
            continue;
        } else if let Some(opening_fence) = markdown_fence_start(line) {
            fence = Some(opening_fence);
            continue;
        }
        if in_comment {
            if trimmed.contains("-->") {
                in_comment = false;
            }
            continue;
        }
        if trimmed.contains("<!--") {
            if !trimmed.contains("-->") {
                in_comment = true;
            }
            continue;
        }
        if trimmed.is_empty() {
            continue;
        }
        if is_thematic_break(trimmed) {
            continue;
        }
        if let Some(heading) = heading_title(trimmed) {
            current_heading = Some(heading);
            continue;
        }
        // Conflict/gap sections stay in the stream so their claims classify
        // as ambiguous; other ignored sections carry metadata, not claims.
        if ignored_claim_line(trimmed)
            || (!is_conflict_gap_heading(current_heading.as_deref())
                && ignored_claim_section(current_heading.as_deref(), options))
        {
            continue;
        }
        let text = trimmed
            .strip_prefix("- ")
            .or_else(|| trimmed.strip_prefix("* "))
            .or_else(|| trimmed.strip_prefix("+ "))
            .unwrap_or(trimmed)
            .trim();
        if text.is_empty() {
            continue;
        }
        claims.push(ClaimLine {
            line: line_number(&page.markdown, line_start),
            heading: current_heading.clone(),
            text: text.to_string(),
            kind: claim_kind(text),
        });
    }

    claims
}

fn claim_kind(text: &str) -> ClaimKind {
    if is_structural_codewiki_claim(text) {
        ClaimKind::Structural
    } else {
        ClaimKind::Prose
    }
}

fn is_structural_codewiki_claim(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    lower.starts_with("module:")
        || lower.starts_with("parent:")
        || lower.starts_with("signature:")
        || lower.starts_with("source path:")
        || lower.starts_with("component:")
        || lower.starts_with("components:")
        || lower.starts_with("[[code/")
}

fn heading_title(line: &str) -> Option<String> {
    parse_atx_heading(line)
        .map(|(_, heading)| heading)
        .filter(|heading| !heading.is_empty())
}

fn ignored_claim_section(heading: Option<&str>, options: &AuditOptions) -> bool {
    heading.is_some_and(|heading| options.ignores_section(heading))
}

/// Sections that record explicitly flagged uncertainty from the
/// accepted-note contract (`conflict:` / `gap:` note lines render here).
const CONFLICT_GAP_SECTIONS: &[&str] = &["conflicting claims", "missing evidence"];

fn is_conflict_gap_heading(heading: Option<&str>) -> bool {
    heading.is_some_and(|heading| {
        CONFLICT_GAP_SECTIONS.contains(&heading.trim().to_ascii_lowercase().as_str())
    })
}

fn has_conflict_gap_prefix(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    [
        "conflict:",
        "conflicting claim:",
        "gap:",
        "missing evidence:",
    ]
    .iter()
    .any(|prefix| lower.starts_with(prefix))
}

fn ignored_claim_line(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.starts_with("sources:")
        || lower.starts_with("source path:")
        || lower.starts_with("citation:")
        || lower.starts_with("citations:")
        || lower == "- none recorded."
}

fn is_thematic_break(line: &str) -> bool {
    let compact = line
        .chars()
        .filter(|ch| !ch.is_whitespace())
        .collect::<String>();
    if compact.len() < 3 {
        return false;
    }
    let Some(marker @ ('-' | '*' | '_')) = compact.chars().next() else {
        return false;
    };
    compact.chars().all(|ch| ch == marker)
}

pub(super) fn has_inline_source_support(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.contains("[[knowledge/sources/")
        || lower.contains("(knowledge/sources/")
        || has_code_source_span(line)
        || has_link_like_source_token(&lower, "citation:")
        || has_link_like_source_token(&lower, "source:")
        || lower.contains("gwiki-source:")
}

fn has_code_source_span(line: &str) -> bool {
    let mut rest = line;
    while let Some(open_index) = rest.find('[') {
        let after_open = &rest[open_index + 1..];
        let Some(close_index) = after_open.find(']') else {
            return false;
        };
        if is_code_source_span(&after_open[..close_index]) {
            return true;
        }
        rest = &after_open[close_index + 1..];
    }
    false
}

fn is_code_source_span(candidate: &str) -> bool {
    let Some((path, span)) = candidate.rsplit_once(':') else {
        return false;
    };
    is_code_source_path(path) && is_line_span(span)
}

fn is_code_source_path(path: &str) -> bool {
    !path.is_empty()
        && !path.contains(char::is_whitespace)
        && (path.contains('/') || path.contains('.'))
        && !path.contains("://")
}

fn is_line_span(span: &str) -> bool {
    let Some((start, end)) = span.split_once('-') else {
        return span.parse::<usize>().is_ok_and(|line| line > 0);
    };
    let Ok(start) = start.parse::<usize>() else {
        return false;
    };
    let Ok(end) = end.parse::<usize>() else {
        return false;
    };
    start > 0 && end >= start
}

fn has_link_like_source_token(line: &str, token: &str) -> bool {
    let mut start = 0;
    while let Some(relative_index) = line[start..].find(token) {
        let index = start + relative_index;
        let before = line[..index].chars().next_back();
        let has_boundary =
            before.is_none_or(|ch| !(ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-')));
        let after = line[index + token.len()..].trim_start();
        if has_boundary
            && (after.starts_with("http://")
                || after.starts_with("https://")
                || after.starts_with("[[knowledge/sources/")
                || after.starts_with('[')
                || after.starts_with("(knowledge/sources/")
                || after.starts_with("knowledge/sources/")
                || after.starts_with("gwiki-source:"))
        {
            return true;
        }
        start = index + token.len();
    }
    false
}
