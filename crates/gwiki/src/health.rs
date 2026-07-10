use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use aho_corasick::AhoCorasick;
use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use gobby_core::vault::links::{LinkKind, canonical_target_key, normalize_wiki_path};
use gobby_core::vault::lint::link_lookup_keys;
use serde::Serialize;

use crate::credibility::{
    CredibilityScore, PageConfidence, PageConfidenceInput, credibility_input_for_source,
    half_life_days_for_content,
};
use crate::lint::{WikiPage, collect_pages, page_match_keys, report_from_pages, title_for_page};
use crate::markdown::{MarkdownFence, markdown_fence_closes, markdown_fence_start};
use crate::provenance::ProvenanceGraph;
use crate::sources::{CompileStatus, SourceManifest, SourceRecord};
use crate::{ScopeIdentity, WikiError};

const AVERAGE_GREGORIAN_YEAR_SECONDS: u64 = 31_556_952;
const STALE_CITATION_YEARS_ENV: &str = "GWIKI_STALE_CITATION_YEARS";
/// Composed page confidence below this is surfaced as low-confidence.
const LOW_CONFIDENCE_THRESHOLD: u8 = 40;
/// Cap on low-confidence entries listed in the report; the summary always
/// carries the full count.
const LOW_CONFIDENCE_LIST_CAP: usize = 10;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HealthReport {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub root: PathBuf,
    pub stale_pages: Vec<PathBuf>,
    pub stale_citations: Vec<HealthSourceIssue>,
    pub uncited_sources: Vec<HealthSourceIssue>,
    pub broken_links: Vec<crate::lint::LinkIssue>,
    pub duplicate_concepts: Vec<DuplicateConcept>,
    pub duplicate_sources: Vec<DuplicateSource>,
    pub uncompiled_sources: Vec<HealthSourceIssue>,
    pub page_confidence: PageConfidenceSummary,
    pub json_path: PathBuf,
    pub text_path: PathBuf,
}

/// Derived page-level confidence over knowledge synthesis pages (concepts and
/// topics). Recomputed from the vault on every health run — never persisted
/// as authoritative page state.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct PageConfidenceSummary {
    pub scored_pages: usize,
    pub average_score: Option<u8>,
    pub low_confidence_count: usize,
    /// Lowest-scoring pages below [`LOW_CONFIDENCE_THRESHOLD`], capped at
    /// [`LOW_CONFIDENCE_LIST_CAP`]; `low_confidence_count` is the full count.
    pub low_confidence: Vec<PageConfidenceIssue>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PageConfidenceIssue {
    pub path: PathBuf,
    pub score: u8,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HealthSourceIssue {
    pub source_id: String,
    pub path: Option<PathBuf>,
    pub location: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DuplicateConcept {
    pub title: String,
    pub paths: Vec<PathBuf>,
}

/// Multiple `knowledge/sources/` pages that resolve to the same canonical source
/// identity — orphaned duplicates left when a recompile minted a slug-suffixed
/// sibling instead of overwriting the derived page in place (#17707).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DuplicateSource {
    pub identity: String,
    pub paths: Vec<PathBuf>,
}

pub fn run(vault_root: &Path, scope: ScopeIdentity) -> Result<HealthReport, WikiError> {
    let pages = collect_pages(vault_root)?;
    let report = report_from_pages_for_health(vault_root, scope.clone(), &pages)?;
    demote_stale_lifecycle(vault_root, &scope, &pages)?;
    persist_report(vault_root, &report)?;
    Ok(report)
}

/// Health owns the `stale` lifecycle demotion: every page detected stale whose
/// lifecycle has not already reached `stale`/`archived` is rewritten through
/// the mark-stale frontmatter path (preserving an existing `stale_reason`) and
/// the transition is appended to `log.md`. [`inspect`] stays read-only for
/// trust/librarian callers.
fn demote_stale_lifecycle(
    vault_root: &Path,
    scope: &ScopeIdentity,
    pages: &[WikiPage],
) -> Result<(), WikiError> {
    use crate::frontmatter::WikiLifecycle;

    for page in pages {
        let frontmatter = &page.parsed.frontmatter;
        if matches!(
            frontmatter.lifecycle,
            Some(WikiLifecycle::Stale | WikiLifecycle::Archived)
        ) {
            continue;
        }
        if !page_is_stale(page) {
            continue;
        }
        let reason = frontmatter
            .unknown
            .get("stale_reason")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("health: page detected stale")
            .to_string();
        crate::lifecycle::apply_lifecycle_transition(
            vault_root,
            scope,
            &page.relative_path,
            WikiLifecycle::Stale,
            &reason,
        )?;
    }
    Ok(())
}

pub fn inspect(vault_root: &Path, scope: ScopeIdentity) -> Result<HealthReport, WikiError> {
    let pages = collect_pages(vault_root)?;
    report_from_pages_for_health(vault_root, scope, &pages)
}

fn report_from_pages_for_health(
    vault_root: &Path,
    scope: ScopeIdentity,
    pages: &[WikiPage],
) -> Result<HealthReport, WikiError> {
    let lint_report = report_from_pages(vault_root, scope.clone(), pages);
    let manifest = SourceManifest::read(vault_root)?;
    let provenance = load_provenance(vault_root)?;
    let needle_index = build_source_needle_index(&manifest.entries);
    let citation_index = build_citation_index(&manifest.entries, pages, &provenance, &needle_index);
    let page_confidence =
        page_confidence_summary(pages, &manifest.entries, &provenance, &needle_index);
    let stale_pages = stale_pages(pages);
    let stale_citations = manifest
        .entries
        .iter()
        .filter(|entry| source_citation_is_stale(entry))
        .map(source_issue)
        .collect();
    let uncited_sources = manifest
        .entries
        .iter()
        .filter(|entry| !citation_index.cites(&entry.id))
        .map(source_issue)
        .collect();
    let duplicate_concepts = duplicate_concepts(pages);
    let duplicate_sources = duplicate_sources(pages);
    let uncompiled_sources = manifest
        .entries
        .iter()
        .filter(|entry| entry.compile_status == CompileStatus::Pending)
        .map(source_issue)
        .collect();
    let report = HealthReport {
        command: "health",
        scope,
        root: vault_root.to_path_buf(),
        stale_pages,
        stale_citations,
        uncited_sources,
        broken_links: lint_report.broken_links,
        duplicate_concepts,
        duplicate_sources,
        uncompiled_sources,
        page_confidence,
        json_path: PathBuf::from("meta/health/latest.json"),
        text_path: PathBuf::from("meta/health/latest.md"),
    };
    Ok(report)
}

/// Compose derived confidence for knowledge synthesis pages (concepts and
/// topics), keyed by relative path: cited-source credibility resolved through
/// the citation needle index, half-life freshness from the page file's age,
/// and backlinks counted from every other vault page's resolved links. Shared
/// by the health summary and the agent page export (#17730).
pub(crate) fn page_confidence_by_path(
    pages: &[WikiPage],
    sources: &[SourceRecord],
    provenance: &ProvenanceGraph,
    needle_index: &SourceNeedleIndex,
) -> BTreeMap<PathBuf, u8> {
    let source_scores: BTreeMap<&str, u8> = sources
        .iter()
        .map(|source| {
            let score =
                CredibilityScore::evaluate(credibility_input_for_source(source, provenance));
            (source.id.as_str(), score.score)
        })
        .collect();

    let scored_pages: Vec<(usize, BTreeSet<String>)> = pages
        .iter()
        .enumerate()
        .filter(|(_, page)| {
            page.relative_path.starts_with("knowledge/concepts")
                || page.relative_path.starts_with("knowledge/topics")
        })
        .map(|(index, page)| (index, page_match_keys(page)))
        .collect();
    if scored_pages.is_empty() {
        return BTreeMap::new();
    }

    let mut key_to_slot: BTreeMap<&str, usize> = BTreeMap::new();
    for (slot, (_, keys)) in scored_pages.iter().enumerate() {
        for key in keys {
            key_to_slot.entry(key.as_str()).or_insert(slot);
        }
    }
    let mut referrers: Vec<BTreeSet<&Path>> = vec![BTreeSet::new(); scored_pages.len()];
    for (page_index, page) in pages.iter().enumerate() {
        for link in &page.parsed.links {
            let key = canonical_target_key(&link.target);
            let Some(&slot) = key_to_slot.get(key.as_str()) else {
                continue;
            };
            if scored_pages[slot].0 == page_index {
                continue;
            }
            referrers[slot].insert(page.relative_path.as_path());
        }
    }

    let mut scores = BTreeMap::new();
    for (slot, (page_index, _)) in scored_pages.iter().enumerate() {
        let page = &pages[*page_index];
        let cited_scores: Vec<u8> = page_cited_source_ids(page, needle_index)
            .iter()
            .filter_map(|source_id| source_scores.get(source_id.as_str()).copied())
            .collect();
        let confidence = PageConfidence::compose(PageConfidenceInput {
            source_scores: cited_scores,
            age_days: page_age_days(&page.path),
            half_life_days: half_life_days_for_content(&page.relative_path),
            backlink_count: referrers[slot].len(),
        });
        scores.insert(page.relative_path.clone(), confidence.score);
    }
    scores
}

/// Health-report roll-up of [`page_confidence_by_path`]: scored-page count,
/// average, and the capped low-confidence list.
fn page_confidence_summary(
    pages: &[WikiPage],
    sources: &[SourceRecord],
    provenance: &ProvenanceGraph,
    needle_index: &SourceNeedleIndex,
) -> PageConfidenceSummary {
    let scores = page_confidence_by_path(pages, sources, provenance, needle_index);
    if scores.is_empty() {
        return PageConfidenceSummary::default();
    }

    let total: u32 = scores.values().map(|score| u32::from(*score)).sum();
    let average_score = Some((total as f64 / scores.len() as f64).round() as u8);
    let mut low_confidence: Vec<PageConfidenceIssue> = scores
        .iter()
        .filter(|(_, score)| **score < LOW_CONFIDENCE_THRESHOLD)
        .map(|(path, score)| PageConfidenceIssue {
            path: path.clone(),
            score: *score,
        })
        .collect();
    low_confidence.sort_by(|left, right| {
        left.score
            .cmp(&right.score)
            .then_with(|| left.path.cmp(&right.path))
    });
    let low_confidence_count = low_confidence.len();
    low_confidence.truncate(LOW_CONFIDENCE_LIST_CAP);

    PageConfidenceSummary {
        scored_pages: scores.len(),
        average_score,
        low_confidence_count,
        low_confidence,
    }
}

/// Page age in days from the file's last modification time.
fn page_age_days(path: &Path) -> Option<u16> {
    let modified = fs::metadata(path).ok()?.modified().ok()?;
    let elapsed = modified.elapsed().ok()?;
    let days = elapsed.as_secs() / 86_400;
    Some(days.min(u64::from(u16::MAX)) as u16)
}

pub fn render_text(report: &HealthReport) -> String {
    let mut text = format!("# Wiki health report\n\nScope: {}\n", report.scope);
    render_paths(&mut text, "Stale pages", &report.stale_pages);
    render_sources(&mut text, "Stale citations", &report.stale_citations);
    render_sources(&mut text, "Uncited sources", &report.uncited_sources);
    render_broken_links(&mut text, &report.broken_links);
    render_duplicate_concepts(&mut text, &report.duplicate_concepts);
    render_duplicate_sources(&mut text, &report.duplicate_sources);
    render_sources(&mut text, "Uncompiled sources", &report.uncompiled_sources);
    render_page_confidence(&mut text, &report.page_confidence);
    text
}

fn render_page_confidence(text: &mut String, summary: &PageConfidenceSummary) {
    text.push_str("\nPage confidence:\n");
    text.push_str(&format!("- scored pages: {}\n", summary.scored_pages));
    match summary.average_score {
        Some(average) => text.push_str(&format!("- average score: {average}\n")),
        None => text.push_str("- average score: none\n"),
    }
    text.push_str(&format!(
        "- low-confidence pages (score < {LOW_CONFIDENCE_THRESHOLD}): {}\n",
        summary.low_confidence_count
    ));
    for issue in &summary.low_confidence {
        text.push_str(&format!("  - {} ({})\n", issue.path.display(), issue.score));
    }
    if summary.low_confidence_count > summary.low_confidence.len() {
        text.push_str(&format!(
            "  - … and {} more\n",
            summary.low_confidence_count - summary.low_confidence.len()
        ));
    }
}

fn persist_report(vault_root: &Path, report: &HealthReport) -> Result<(), WikiError> {
    let health_dir = vault_root.join("meta").join("health");
    fs::create_dir_all(&health_dir).map_err(|error| WikiError::Io {
        action: "create health report directory",
        path: Some(health_dir.clone()),
        source: error,
    })?;
    let json_path = vault_root.join(&report.json_path);
    let text_path = vault_root.join(&report.text_path);
    let json = serde_json::to_string_pretty(report).map_err(|error| WikiError::Json {
        action: "serialize health report",
        path: Some(json_path.clone()),
        source: error,
    })?;
    fs::write(&json_path, json).map_err(|error| WikiError::Io {
        action: "write health JSON report",
        path: Some(json_path),
        source: error,
    })?;
    fs::write(&text_path, render_text(report)).map_err(|error| WikiError::Io {
        action: "write health text report",
        path: Some(text_path),
        source: error,
    })
}

fn stale_pages(pages: &[crate::lint::WikiPage]) -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = pages
        .iter()
        .filter(|page| page_is_stale(page))
        .map(|page| page.relative_path.clone())
        .collect();
    paths.sort();
    paths
}

fn page_is_stale(page: &crate::lint::WikiPage) -> bool {
    let frontmatter = &page.parsed.frontmatter;
    if frontmatter
        .unknown
        .get("stale")
        .and_then(serde_json::Value::as_bool)
        == Some(true)
    {
        return true;
    }
    for key in ["status", "review_status"] {
        if frontmatter
            .unknown
            .get(key)
            .and_then(serde_json::Value::as_str)
            .is_some_and(|value| value.eq_ignore_ascii_case("stale"))
        {
            return true;
        }
    }
    frontmatter
        .unknown
        .get("stale_after")
        .and_then(serde_json::Value::as_str)
        .is_some_and(|value| stale_after_is_due(value, Utc::now()))
}

fn stale_after_is_due(value: &str, now: DateTime<Utc>) -> bool {
    let value = value.trim();
    if value.is_empty() {
        return false;
    }
    if let Ok(parsed) = DateTime::parse_from_rfc3339(value) {
        return parsed.with_timezone(&Utc) <= now;
    }
    if let Ok(parsed) = NaiveDate::parse_from_str(value, "%Y-%m-%d") {
        return parsed <= now.date_naive();
    }
    for format in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(value, format) {
            return parsed.and_utc() <= now;
        }
    }
    false
}

fn source_citation_is_stale(source: &SourceRecord) -> bool {
    source_citation_is_stale_at(source, Utc::now())
}

fn source_citation_is_stale_at(source: &SourceRecord, now: DateTime<Utc>) -> bool {
    let stale_years = stale_citation_years();
    source.citation.is_some() && fetched_at_is_stale(&source.fetched_at, stale_years, now)
}

fn fetched_at_is_stale(value: &str, stale_years: u64, now: DateTime<Utc>) -> bool {
    if let Some(fetched_at) = parse_fetched_at(value) {
        let stale_seconds = stale_years.saturating_mul(AVERAGE_GREGORIAN_YEAR_SECONDS);
        let Ok(stale_seconds) = i64::try_from(stale_seconds) else {
            return false;
        };
        return fetched_at
            .checked_add_signed(chrono::Duration::seconds(stale_seconds))
            .is_some_and(|deadline| deadline <= now);
    }
    fetched_year(value)
        .is_some_and(|year| year.saturating_add(stale_years) < approximate_current_year_at(now))
}

fn parse_fetched_at(value: &str) -> Option<DateTime<Utc>> {
    if let Ok(parsed) = DateTime::parse_from_rfc3339(value) {
        return Some(parsed.with_timezone(&Utc));
    }
    if let Ok(parsed) = NaiveDate::parse_from_str(value, "%Y-%m-%d") {
        return parsed.and_hms_opt(0, 0, 0).map(|value| value.and_utc());
    }
    for format in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(value, format) {
            return Some(parsed.and_utc());
        }
    }
    None
}

fn stale_citation_years() -> u64 {
    match std::env::var(STALE_CITATION_YEARS_ENV) {
        Ok(raw) => stale_citation_years_from_env(&raw).unwrap_or_else(|| {
            eprintln!("warning: ignoring invalid {STALE_CITATION_YEARS_ENV}={raw}");
            1
        }),
        Err(_) => 1,
    }
}

fn stale_citation_years_from_env(raw: &str) -> Option<u64> {
    raw.trim().parse::<u64>().ok().filter(|value| *value > 0)
}

fn fetched_year(value: &str) -> Option<u64> {
    let year = value.get(0..4)?;
    (year.chars().all(|ch| ch.is_ascii_digit()))
        .then(|| year.parse().ok())
        .flatten()
}

fn approximate_current_year_at(now: DateTime<Utc>) -> u64 {
    // Health checks only need a coarse stale-citation window; using the average
    // Gregorian year keeps this dependency-free and avoids timezone handling.
    1970 + u64::try_from(now.timestamp()).unwrap_or(0) / AVERAGE_GREGORIAN_YEAR_SECONDS
}

pub(crate) fn load_provenance(vault_root: &Path) -> Result<ProvenanceGraph, WikiError> {
    let path = vault_root.join("meta").join("provenance.json");
    if path.exists() {
        ProvenanceGraph::load_from_vault(vault_root)
    } else {
        Ok(ProvenanceGraph::default())
    }
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn change_triggered_affected_pages(
    vault_root: &Path,
    graph_config: Option<&gobby_core::config::FalkorConfig>,
    project: &str,
    changes: crate::code_graph::CodeChangeSet,
) -> Result<crate::code_graph::AffectedPages, WikiError> {
    let provenance = load_provenance(vault_root)?;
    crate::code_graph::affected_pages_for_changes(graph_config, project, &provenance, changes)
        .map_err(|error| WikiError::Config {
            detail: format!("query change-triggered affected pages: {error}"),
        })
}

#[derive(Default)]
struct SourceCitationIndex {
    cited_source_ids: BTreeSet<String>,
}

impl SourceCitationIndex {
    fn cites(&self, source_id: &str) -> bool {
        self.cited_source_ids.contains(source_id)
    }
}

pub(crate) struct SourceNeedleIndex {
    text_patterns: Vec<String>,
    text_source_ids: Vec<BTreeSet<String>>,
    link_source_ids_by_target: BTreeMap<String, BTreeSet<String>>,
}

fn build_citation_index(
    sources: &[SourceRecord],
    pages: &[crate::lint::WikiPage],
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
fn page_cited_source_ids(
    page: &crate::lint::WikiPage,
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

#[cfg(test)]
fn source_reference_is_present(markdown: &str, needle: &str) -> bool {
    let needle = needle.trim();
    if needle.is_empty() {
        return false;
    }
    let markdown = markdown_without_fenced_code(markdown);
    let matcher = AhoCorasick::new([needle]).expect("single pattern matcher builds");
    matcher
        .find_overlapping_iter(&markdown)
        .any(|matched| has_text_match_boundaries(&markdown, matched.start(), matched.end()))
}

fn markdown_without_fenced_code(markdown: &str) -> String {
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

fn has_text_match_boundaries(markdown: &str, start: usize, end: usize) -> bool {
    let before = markdown[..start].chars().next_back();
    let after = markdown[end..].chars().next();
    !before.is_some_and(is_citation_word_char) && !after.is_some_and(is_citation_word_char)
}

fn is_citation_word_char(value: char) -> bool {
    value == '_' || value.is_alphanumeric()
}

fn source_issue(source: &SourceRecord) -> HealthSourceIssue {
    HealthSourceIssue {
        source_id: source.id.clone(),
        path: Some(PathBuf::from("raw").join(format!("{}.md", source.id))),
        location: source.location.clone(),
    }
}

fn duplicate_concepts(pages: &[crate::lint::WikiPage]) -> Vec<DuplicateConcept> {
    let mut by_title: BTreeMap<String, (String, Vec<PathBuf>)> = BTreeMap::new();
    for page in pages {
        if !page.relative_path.starts_with("knowledge/concepts") {
            continue;
        }
        let title = title_for_page(page);
        by_title
            .entry(title.to_ascii_lowercase())
            .or_insert_with(|| (title, Vec::new()))
            .1
            .push(page.relative_path.clone());
    }
    by_title
        .into_values()
        .filter_map(|(title, mut paths)| {
            paths.sort();
            (paths.len() > 1).then_some(DuplicateConcept { title, paths })
        })
        .collect()
}

fn render_paths(text: &mut String, heading: &str, paths: &[PathBuf]) {
    text.push('\n');
    text.push_str(heading);
    text.push_str(":\n");
    if paths.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for path in paths {
        text.push_str("- ");
        text.push_str(&path.display().to_string());
        text.push('\n');
    }
}

fn render_sources(text: &mut String, heading: &str, sources: &[HealthSourceIssue]) {
    text.push('\n');
    text.push_str(heading);
    text.push_str(":\n");
    if sources.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for source in sources {
        text.push_str("- ");
        text.push_str(&source.source_id);
        text.push_str(" (");
        text.push_str(&source.location);
        text.push_str(")\n");
    }
}

fn render_broken_links(text: &mut String, issues: &[crate::lint::LinkIssue]) {
    text.push_str("\nBroken links:\n");
    if issues.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for issue in issues {
        text.push_str("- ");
        text.push_str(&issue.path.display().to_string());
        text.push(':');
        text.push_str(&issue.line.to_string());
        text.push_str(" -> ");
        text.push_str(&issue.target);
        text.push('\n');
    }
}

fn render_duplicate_concepts(text: &mut String, duplicates: &[DuplicateConcept]) {
    text.push_str("\nDuplicate concepts:\n");
    if duplicates.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for duplicate in duplicates {
        text.push_str("- ");
        text.push_str(&duplicate.title);
        text.push_str(": ");
        text.push_str(
            &duplicate
                .paths
                .iter()
                .map(|path| path.display().to_string())
                .collect::<Vec<_>>()
                .join(", "),
        );
        text.push('\n');
    }
}

/// Group `knowledge/sources/` pages by `(canonical source identity, title)` and
/// flag any pairing backed by more than one page. A base/-2/-3 sibling set for
/// one re-fetched source shares both keys (the `-N` suffix lives only in the
/// filename slug, never the title), so it collapses to a single duplicate group.
/// Keying on both fields keeps genuinely distinct sources apart even when they
/// coincide on one field: different repos that share a title keep distinct
/// identities, and distinct notes captured from a shared directory location
/// (same identity slug) keep distinct titles (#17707).
fn duplicate_sources(pages: &[crate::lint::WikiPage]) -> Vec<DuplicateSource> {
    let mut by_source: BTreeMap<(String, String), Vec<PathBuf>> = BTreeMap::new();
    for page in pages {
        if !page.relative_path.starts_with("knowledge/sources") {
            continue;
        }
        let Some(identity) = crate::synthesis::page_source_identities(&page.markdown)
            .into_iter()
            .next()
        else {
            continue;
        };
        let key = (
            crate::synthesis::source_identity_key(&identity),
            title_for_page(page),
        );
        by_source
            .entry(key)
            .or_default()
            .push(page.relative_path.clone());
    }
    by_source
        .into_iter()
        .filter_map(|((identity, _title), mut paths)| {
            paths.sort();
            (paths.len() > 1).then_some(DuplicateSource { identity, paths })
        })
        .collect()
}

fn render_duplicate_sources(text: &mut String, duplicates: &[DuplicateSource]) {
    text.push_str("\nDuplicate source pages:\n");
    if duplicates.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for duplicate in duplicates {
        text.push_str("- ");
        text.push_str(&duplicate.identity);
        text.push_str(": ");
        text.push_str(
            &duplicate
                .paths
                .iter()
                .map(|path| path.display().to_string())
                .collect::<Vec<_>>()
                .join(", "),
        );
        text.push('\n');
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::frontmatter::{WikiLifecycle, parse_frontmatter};
    use crate::sources::{IngestionMethod, SourceDraft, SourceKind, SourceManifest};

    #[test]
    fn run_demotes_stale_detected_pages_to_stale_lifecycle() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_page(
            temp.path(),
            "knowledge/concepts/aging.md",
            "---\ntitle: Aging\nlifecycle: verified\nstale_after: 2020-01-01\n---\n\nBody.\n",
        );
        write_page(
            temp.path(),
            "knowledge/concepts/fresh.md",
            "---\ntitle: Fresh\nlifecycle: verified\n---\n\nBody.\n",
        );

        let report = run(temp.path(), ScopeIdentity::project("proj")).expect("health run succeeds");
        assert_eq!(
            report.stale_pages,
            vec![PathBuf::from("knowledge/concepts/aging.md")]
        );

        let aging = std::fs::read_to_string(temp.path().join("knowledge/concepts/aging.md"))
            .expect("read aging page");
        let parsed = parse_frontmatter(&aging).expect("parse aging page");
        assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Stale));
        assert!(parsed.metadata.unknown.contains_key("stale_at"));

        let fresh = std::fs::read_to_string(temp.path().join("knowledge/concepts/fresh.md"))
            .expect("read fresh page");
        let parsed_fresh = parse_frontmatter(&fresh).expect("parse fresh page");
        assert_eq!(
            parsed_fresh.metadata.lifecycle,
            Some(WikiLifecycle::Verified)
        );

        let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
        assert!(log.contains("lifecycle_transition:"), "{log}");
        assert!(log.contains("verified -> stale"), "{log}");

        // Re-running is idempotent: the page is already stale, no second
        // transition is logged.
        run(temp.path(), ScopeIdentity::project("proj")).expect("second health run");
        let log_after = std::fs::read_to_string(temp.path().join("log.md")).expect("re-read log");
        assert_eq!(
            log_after.matches("lifecycle_transition:").count(),
            1,
            "{log_after}"
        );
    }

    #[test]
    fn inspect_does_not_demote_lifecycle() {
        let temp = tempfile::tempdir().expect("tempdir");
        let markdown =
            "---\ntitle: Aging\nlifecycle: verified\nstale_after: 2020-01-01\n---\n\nBody.\n";
        write_page(temp.path(), "knowledge/concepts/aging.md", markdown);

        inspect(temp.path(), ScopeIdentity::project("proj")).expect("inspect succeeds");

        let after = std::fs::read_to_string(temp.path().join("knowledge/concepts/aging.md"))
            .expect("read page");
        assert_eq!(after, markdown);
        assert!(!temp.path().join("log.md").exists());
    }

    #[test]
    fn duplicate_sources_flags_rotated_hash_siblings_not_distinct_sources() {
        let temp = tempfile::tempdir().expect("tempdir");
        let sources = temp.path().join("knowledge/sources");
        std::fs::create_dir_all(&sources).expect("sources dir");

        let stub = |source_path: &str, body: &str| {
            format!(
                "---\ntitle: \"Example\"\nsource_kind: \"source_note\"\n\
                 synthesis_mode: \"source\"\nsource_path: \"{source_path}\"\n---\n\n\
                 # Example\n\n{body}\n"
            )
        };
        // Base + rotated-hash sibling for ONE canonical GitHub source: the
        // content hash rotated on re-fetch but the location slug is stable, so
        // both pages describe the same source and are orphaned duplicates.
        std::fs::write(
            sources.join("example-repo.md"),
            stub(
                "raw/src-0000000000000000-https-github-com-example-repo.md",
                "Base.",
            ),
        )
        .expect("base written");
        std::fs::write(
            sources.join("example-repo-2.md"),
            stub(
                "raw/src-1111111111111111-https-github-com-example-repo.md",
                "Sibling.",
            ),
        )
        .expect("sibling written");
        // A genuinely distinct source (different location) must NOT be flagged.
        std::fs::write(
            sources.join("other-repo.md"),
            stub(
                "raw/src-2222222222222222-https-github-com-other-repo.md",
                "Other.",
            ),
        )
        .expect("other written");
        // Two distinct notes captured from the same directory location share an
        // identity slug but differ by title — they must NOT be flagged as
        // duplicates of each other (the false positive an identity-only key hit).
        let titled_stub = |title: &str, source_path: &str| {
            format!(
                "---\ntitle: \"{title}\"\nsource_kind: \"source_note\"\n\
                 synthesis_mode: \"source\"\nsource_path: \"{source_path}\"\n---\n\n# {title}\n"
            )
        };
        std::fs::write(
            sources.join("note-alpha-md.md"),
            titled_stub(
                "note-alpha.md",
                "raw/src-3333333333333333-tmp-scratch-dir.md",
            ),
        )
        .expect("note alpha written");
        std::fs::write(
            sources.join("note-beta-md.md"),
            titled_stub(
                "note-beta.md",
                "raw/src-4444444444444444-tmp-scratch-dir.md",
            ),
        )
        .expect("note beta written");

        let pages = collect_pages(temp.path()).expect("pages collected");
        let duplicates = duplicate_sources(&pages);

        assert_eq!(duplicates.len(), 1, "{duplicates:?}");
        assert_eq!(duplicates[0].identity, "https-github-com-example-repo");
        assert_eq!(
            duplicates[0].paths,
            vec![
                PathBuf::from("knowledge/sources/example-repo-2.md"),
                PathBuf::from("knowledge/sources/example-repo.md"),
            ]
        );
    }

    #[test]
    fn health_checks_required_cases() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let source = SourceManifest::register(
            root,
            SourceDraft::url(
                "https://example.com/uncited",
                "2026-05-29T12:00:00Z",
                "uncited source",
            )
            .with_citation("Uncited Example"),
        )
        .expect("source registered");
        write_page(
            root,
            "knowledge/topics/stale.md",
            "---\ntitle: Stale\nstale: true\n---\n# Stale\nSee [[Missing]].\n",
        );
        write_page(
            root,
            "knowledge/concepts/cache-a.md",
            "---\ntitle: Cache\nsource_kind: concept\n---\n# Cache\nConcept A.\n",
        );
        write_page(
            root,
            "knowledge/concepts/cache-b.md",
            "---\ntitle: Cache\nsource_kind: concept\n---\n# Cache\nConcept B.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

        assert_eq!(
            report.stale_pages,
            vec![PathBuf::from("knowledge/topics/stale.md")]
        );
        assert_eq!(report.uncited_sources[0].source_id, source.id);
        assert_eq!(report.broken_links[0].target, "Missing");
        assert_eq!(report.duplicate_concepts[0].title, "Cache");
        assert_eq!(report.uncompiled_sources[0].source_id, source.id);
        assert!(root.join("meta/health/latest.json").exists());
        let markdown =
            std::fs::read_to_string(root.join("meta/health/latest.md")).expect("health markdown");
        assert!(markdown.starts_with("# Wiki health report\n\nScope: topic:ops\n"));
    }

    #[test]
    fn health_composes_page_confidence_for_knowledge_pages() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let source = SourceManifest::register(
            root,
            SourceDraft::url(
                "https://example.com/dispatch",
                Utc::now().to_rfc3339(),
                "dispatch source",
            )
            .with_citation("Dispatch Design Notes"),
        )
        .expect("source registered");
        write_page(
            root,
            "knowledge/concepts/dispatch.md",
            &format!(
                "---\ntitle: Dispatch\n---\n# Dispatch\nGrounded in [[knowledge/sources/{}]].\n",
                source.id
            ),
        );
        write_page(
            root,
            "knowledge/topics/automation.md",
            "---\ntitle: Automation\n---\n# Automation\nBuilt on [[Dispatch]].\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

        // Both knowledge synthesis pages are scored; freshly written pages
        // with a cited source and a backlink stay above the base score, so
        // nothing lands in the low-confidence list.
        let summary = &report.page_confidence;
        assert_eq!(summary.scored_pages, 2, "{summary:?}");
        let average = summary.average_score.expect("average confidence");
        assert!(
            average > 50,
            "fresh cited pages must average above the base score: {average}"
        );
        assert_eq!(summary.low_confidence_count, 0, "{summary:?}");
        assert!(summary.low_confidence.is_empty(), "{summary:?}");

        let markdown =
            std::fs::read_to_string(root.join("meta/health/latest.md")).expect("health markdown");
        assert!(markdown.contains("Page confidence:"), "{markdown}");
        assert!(markdown.contains("- scored pages: 2"), "{markdown}");
    }

    #[test]
    fn inspect_does_not_persist_health_snapshots() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        SourceManifest::register(
            root,
            SourceDraft::url(
                "https://example.com/source",
                "2026-05-29T12:00:00Z",
                "source",
            )
            .with_citation("Example Source"),
        )
        .expect("source registered");
        write_page(
            root,
            "knowledge/topics/page.md",
            "# Page\nSee raw/INDEX.md.\n",
        );

        let report = inspect(root, ScopeIdentity::topic("ops")).expect("health inspects");

        assert_eq!(report.command, "health");
        assert!(!root.join("meta/health/latest.json").exists());
        assert!(!root.join("meta/health/latest.md").exists());
    }

    #[test]
    fn source_reference_matching_skips_code_fences_and_partial_words() {
        assert!(!source_reference_is_present(
            "```md\nhttps://example.test/source\n```\n",
            "https://example.test/source"
        ));
        assert!(!source_reference_is_present(
            "prefixsource-idsuffix",
            "source-id"
        ));
        assert!(source_reference_is_present(
            "[Example](https://example.test/source)",
            "https://example.test/source"
        ));
    }

    #[test]
    fn citation_index_marks_cited_sources_once_per_page() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let cited = SourceManifest::register(
            root,
            SourceDraft::url(
                "https://example.com/cited",
                "2026-05-29T12:00:00Z",
                "cited source",
            )
            .with_citation("Cited Example"),
        )
        .expect("cited source registered");
        let uncited = SourceManifest::register(
            root,
            SourceDraft::url(
                "https://example.com/uncited",
                "2026-05-29T12:00:00Z",
                "uncited source",
            )
            .with_citation("Uncited Example"),
        )
        .expect("uncited source registered");
        write_page(
            root,
            "knowledge/topics/cited.md",
            "# Cited\n\n[Cited Example](https://example.com/cited)\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");
        let uncited_ids = report
            .uncited_sources
            .iter()
            .map(|issue| issue.source_id.as_str())
            .collect::<Vec<_>>();

        assert!(!uncited_ids.contains(&cited.id.as_str()));
        assert!(uncited_ids.contains(&uncited.id.as_str()));
    }

    #[test]
    fn citation_index_survives_real_vault_scale() {
        // Regression: the citation regex set once exceeded the regex crate's
        // compiled-size limit at ~200 sources (each pattern repeating the
        // unicode boundary classes), silently degrading the index to
        // provenance-only and reporting every text-cited source as uncited.
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let mut cited_id = String::new();
        for index in 0..512 {
            let source = SourceManifest::register(
                root,
                SourceDraft::url(
                    format!(
                        "session:0000{index:04}-4238-48bf-9edd-07ce27e3c481-{index:04}-long-id"
                    ),
                    "2026-05-29T12:00:00Z",
                    format!("session source {index}"),
                )
                .with_citation(format!("session:citation-{index:04}")),
            )
            .expect("source registered");
            if index == 150 {
                cited_id = source.id.clone();
            }
        }
        write_page(
            root,
            "recaps/2026-07-05.md",
            &format!(
                "---\ntitle: \"Recap: 2026-07-05\"\nrecap_date: 2026-07-05\n---\n# Recap\n\n\
                 ## Sessions\n\n- [[knowledge/sources/{cited_id}|Session]]\n"
            ),
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

        let uncited_ids = report
            .uncited_sources
            .iter()
            .map(|issue| issue.source_id.as_str())
            .collect::<Vec<_>>();
        assert!(
            !uncited_ids.contains(&cited_id.as_str()),
            "recap link citation must count at 512-source scale"
        );
        assert_eq!(uncited_ids.len(), 511, "only the uncited 511 remain");
    }

    #[test]
    fn recap_page_links_count_as_citations() {
        // recaps/ pages participate in the citation index (#17575): a source
        // whose only reference is its digest link on a daily recap page is
        // cited, not orphaned.
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let source = SourceManifest::register(
            root,
            SourceDraft::url(
                "https://example.com/session",
                "2026-05-29T12:00:00Z",
                "session source",
            )
            .with_citation("session:recap-only"),
        )
        .expect("source registered");
        write_page(
            root,
            "recaps/2026-07-05.md",
            &format!(
                "---\ntitle: \"Recap: 2026-07-05\"\nrecap_date: 2026-07-05\n---\n# Recap\n\n\
                 ## Sessions\n\n- [[knowledge/sources/{}|Session]]\n",
                source.id
            ),
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

        let uncited_ids = report
            .uncited_sources
            .iter()
            .map(|issue| issue.source_id.as_str())
            .collect::<Vec<_>>();
        assert!(
            !uncited_ids.contains(&source.id.as_str()),
            "recap-linked source must count as cited: {uncited_ids:?}"
        );
    }

    #[test]
    fn source_reference_matching_uses_unicode_word_boundaries() {
        assert!(!source_reference_is_present("αsource-id", "source-id"));
        assert!(!source_reference_is_present("source-idβ", "source-id"));
        assert!(!source_reference_is_present("source-id_", "source-id"));
        assert!(!source_reference_is_present("source-id9", "source-id"));
        assert!(source_reference_is_present("source-id.", "source-id"));
    }

    #[test]
    fn stale_after_compares_dates_and_times_to_now() {
        let now = DateTime::parse_from_rfc3339("2026-06-02T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc);

        assert!(stale_after_is_due("2026-06-02", now));
        assert!(stale_after_is_due("2026-06-02T11:59:59Z", now));
        assert!(!stale_after_is_due("2026-06-03", now));
        assert!(!stale_after_is_due("not-a-date", now));
    }

    #[test]
    fn fenced_code_closes_only_on_matching_delimiter() {
        let markdown = "before\n~~~\nhttps://example.test/source\n```\nstill fenced\n~~~\nafter\n";

        let without_fences = markdown_without_fenced_code(markdown);

        assert_eq!(without_fences, "before\nafter\n");
    }

    #[test]
    fn fenced_code_requires_matching_marker_length() {
        let markdown =
            "before\n````\nhttps://example.test/source\n```\nstill fenced\n````\nafter\n";

        let without_fences = markdown_without_fenced_code(markdown);

        assert_eq!(without_fences, "before\nafter\n");
    }

    #[test]
    fn stale_citation_env_rejects_invalid_values() {
        assert_eq!(stale_citation_years_from_env("3"), Some(3));
        assert_eq!(stale_citation_years_from_env(" 2 "), Some(2));
        assert_eq!(stale_citation_years_from_env("0"), None);
        assert_eq!(stale_citation_years_from_env("nope"), None);
    }

    #[test]
    fn stale_citation_uses_full_fetched_timestamp() {
        let now = DateTime::parse_from_rfc3339("2026-06-02T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc);

        assert!(source_citation_is_stale_at(
            &source_record("2025-06-02T05:00:00Z"),
            now
        ));
        assert!(!source_citation_is_stale_at(
            &source_record("2025-06-02T18:00:00Z"),
            now
        ));
    }

    #[test]
    fn change_triggered_refresh_health_degrades_to_provenance_only_mapping() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let mut provenance = ProvenanceGraph::default();
        provenance.add_link(crate::provenance::ProvenanceLink {
            source: crate::provenance::SourceChunkRef {
                source_id: "source-lib".to_string(),
                chunk_id: "source-lib#chunk-0".to_string(),
                path: PathBuf::from("src/lib.rs"),
                byte_start: 0,
                byte_end: 10,
            },
            section: crate::provenance::WikiSectionRef {
                page_path: PathBuf::from("code/lib.md"),
                heading: "Lib".to_string(),
                section_id: "lib".to_string(),
            },
            claim: None,
        });
        provenance.save_to_vault(root).expect("save provenance");

        let affected = change_triggered_affected_pages(
            root,
            None,
            "project-1",
            crate::code_graph::CodeChangeSet {
                files: vec!["src/lib.rs".to_string()],
                symbols: Vec::new(),
            },
        )
        .expect("affected pages");

        assert_eq!(affected.pages.len(), 1);
        assert_eq!(affected.pages[0].page_path, PathBuf::from("code/lib.md"));
        assert_eq!(affected.degradations.len(), 1);
    }

    fn write_page(root: &Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
        std::fs::write(path, markdown).expect("write page");
    }

    fn source_record(fetched_at: &str) -> SourceRecord {
        SourceRecord {
            id: "source-id".to_string(),
            location: "https://example.test/source".to_string(),
            canonical_location: "https://example.test/source".to_string(),
            kind: SourceKind::Url,
            fetched_at: fetched_at.to_string(),
            content_hash: "hash".to_string(),
            title: None,
            citation: Some("Example".to_string()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Compiled,
            replay: None,
        }
    }
}
