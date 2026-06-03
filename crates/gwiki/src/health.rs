use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use linked_hash_map::LinkedHashMap;
use serde::Serialize;

use crate::lint::{collect_pages, title_for_page};
use crate::markdown::{MarkdownFence, markdown_fence_closes, markdown_fence_start};
use crate::provenance::ProvenanceGraph;
use crate::sources::{CompileStatus, SourceManifest, SourceRecord};
use crate::{ScopeIdentity, WikiError};

const AVERAGE_GREGORIAN_YEAR_SECONDS: u64 = 31_556_952;
const STALE_CITATION_YEARS_ENV: &str = "GWIKI_STALE_CITATION_YEARS";
const REGEX_CACHE_CAPACITY: usize = 1_000;

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
    pub uncompiled_sources: Vec<HealthSourceIssue>,
    pub json_path: PathBuf,
    pub text_path: PathBuf,
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

pub fn run(vault_root: &Path, scope: ScopeIdentity) -> Result<HealthReport, WikiError> {
    let lint_report = crate::lint::run(vault_root, scope.clone())?;
    let pages = collect_pages(vault_root)?;
    let manifest = SourceManifest::read(vault_root)?;
    let provenance = load_provenance(vault_root)?;
    let citation_index = build_citation_index(&manifest.entries, &pages, &provenance);
    let stale_pages = stale_pages(&pages);
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
    let duplicate_concepts = duplicate_concepts(&pages);
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
        uncompiled_sources,
        json_path: PathBuf::from("meta/health/latest.json"),
        text_path: PathBuf::from("meta/health/latest.md"),
    };
    persist_report(vault_root, &report)?;
    Ok(report)
}

pub fn render_text(report: &HealthReport) -> String {
    let mut text = format!("Wiki health report\nScope: {}\n", report.scope);
    render_paths(&mut text, "Stale pages", &report.stale_pages);
    render_sources(&mut text, "Stale citations", &report.stale_citations);
    render_sources(&mut text, "Uncited sources", &report.uncited_sources);
    render_broken_links(&mut text, &report.broken_links);
    render_duplicate_concepts(&mut text, &report.duplicate_concepts);
    render_sources(&mut text, "Uncompiled sources", &report.uncompiled_sources);
    text
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

fn load_provenance(vault_root: &Path) -> Result<ProvenanceGraph, WikiError> {
    let path = vault_root.join("meta").join("provenance.json");
    if path.exists() {
        ProvenanceGraph::load_from_vault(vault_root)
    } else {
        Ok(ProvenanceGraph::default())
    }
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

fn build_citation_index(
    sources: &[SourceRecord],
    pages: &[crate::lint::WikiPage],
    provenance: &ProvenanceGraph,
) -> SourceCitationIndex {
    let mut cited_source_ids = sources
        .iter()
        .filter(|source| !provenance.links_for_source(&source.id).is_empty())
        .map(|source| source.id.clone())
        .collect::<BTreeSet<_>>();
    let mut patterns = Vec::new();
    let mut pattern_source_ids = Vec::new();
    for source in sources {
        for needle in source_reference_needles(source) {
            for pattern in source_reference_patterns(needle) {
                patterns.push(pattern);
                pattern_source_ids.push(source.id.as_str());
            }
        }
    }
    if patterns.is_empty() {
        return SourceCitationIndex { cited_source_ids };
    }
    let regex_set = match regex::RegexSet::new(&patterns) {
        Ok(regex_set) => regex_set,
        Err(error) => {
            log::warn!("failed to build health citation regex set: {error}");
            return SourceCitationIndex { cited_source_ids };
        }
    };

    for page in pages {
        let markdown = markdown_without_fenced_code(&page.markdown);
        for matched in regex_set.matches(&markdown) {
            cited_source_ids.insert(pattern_source_ids[matched].to_string());
        }
    }
    SourceCitationIndex { cited_source_ids }
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

fn source_reference_patterns(needle: &str) -> Vec<String> {
    let needle = needle.trim();
    if needle.is_empty() {
        return Vec::new();
    }
    vec![
        markdown_link_target_pattern(needle),
        bounded_text_pattern(needle),
    ]
}

fn source_reference_is_present(markdown: &str, needle: &str) -> bool {
    let needle = needle.trim();
    if needle.is_empty() {
        return false;
    }
    let markdown = markdown_without_fenced_code(markdown);
    markdown_link_target_matches(&markdown, needle) || bounded_text_matches(&markdown, needle)
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

fn markdown_link_target_matches(markdown: &str, needle: &str) -> bool {
    cached_regex_is_match(markdown_link_target_pattern(needle), markdown)
}

fn bounded_text_matches(markdown: &str, needle: &str) -> bool {
    cached_regex_is_match(bounded_text_pattern(needle), markdown)
}

fn markdown_link_target_pattern(needle: &str) -> String {
    let escaped = regex::escape(needle);
    format!(
        r#"(?m)(\[[^\]]*\]\(\s*<?{escaped}>?(?:\s+["'][^"']*["'])?\s*\)|\[\[\s*{escaped}(?:\|[^\]]*)?\s*\]\])"#
    )
}

fn bounded_text_pattern(needle: &str) -> String {
    let escaped = regex::escape(needle);
    format!(r#"(^|[^\p{{L}}\p{{N}}_]){escaped}($|[^\p{{L}}\p{{N}}_])"#)
}

fn cached_regex_is_match(pattern: String, haystack: &str) -> bool {
    static CACHE: OnceLock<Mutex<RegexCache>> = OnceLock::new();
    let mut cache = match CACHE
        .get_or_init(|| Mutex::new(RegexCache::default()))
        .lock()
    {
        Ok(cache) => cache,
        Err(poisoned) => {
            // Regex compilation is deterministic; recovering the cache keeps a
            // prior panic from forcing every later check down the slow path.
            poisoned.into_inner()
        }
    };
    let regex = match cache.get(&pattern) {
        Some(regex) => regex,
        None => {
            let regex = match regex::Regex::new(&pattern) {
                Ok(regex) => regex,
                Err(error) => {
                    log::warn!("invalid health regex pattern `{pattern}`: {error}");
                    return false;
                }
            };
            cache.insert(pattern, regex.clone());
            regex
        }
    };
    drop(cache);
    regex.is_match(haystack)
}

#[derive(Default)]
struct RegexCache {
    entries: LinkedHashMap<String, regex::Regex>,
}

impl RegexCache {
    fn get(&mut self, pattern: &str) -> Option<regex::Regex> {
        let regex = self.entries.remove(pattern)?;
        let cloned = regex.clone();
        self.entries.insert(pattern.to_string(), regex);
        Some(cloned)
    }

    fn insert(&mut self, pattern: String, regex: regex::Regex) {
        self.entries.remove(&pattern);
        self.entries.insert(pattern, regex);
        while self.entries.len() > REGEX_CACHE_CAPACITY {
            self.entries.pop_front();
        }
    }
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
        if !page.relative_path.starts_with("wiki/concepts") {
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{IngestionMethod, SourceDraft, SourceKind, SourceManifest};

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
            "wiki/topics/stale.md",
            "---\ntitle: Stale\nstale: true\n---\n# Stale\nSee [[Missing]].\n",
        );
        write_page(
            root,
            "wiki/concepts/cache-a.md",
            "---\ntitle: Cache\nsource_kind: concept\n---\n# Cache\nConcept A.\n",
        );
        write_page(
            root,
            "wiki/concepts/cache-b.md",
            "---\ntitle: Cache\nsource_kind: concept\n---\n# Cache\nConcept B.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

        assert_eq!(
            report.stale_pages,
            vec![PathBuf::from("wiki/topics/stale.md")]
        );
        assert_eq!(report.uncited_sources[0].source_id, source.id);
        assert_eq!(report.broken_links[0].target, "Missing");
        assert_eq!(report.duplicate_concepts[0].title, "Cache");
        assert_eq!(report.uncompiled_sources[0].source_id, source.id);
        assert!(root.join("meta/health/latest.json").exists());
        assert!(root.join("meta/health/latest.md").exists());
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
            "wiki/topics/cited.md",
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
    fn cached_regex_returns_false_for_malformed_patterns() {
        assert!(!cached_regex_is_match("[".to_string(), "anything"));
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
    fn regex_cache_touch_updates_lru_order() {
        let mut cache = RegexCache::default();
        cache.insert("one".to_string(), regex::Regex::new("one").unwrap());
        cache.insert("two".to_string(), regex::Regex::new("two").unwrap());

        assert!(cache.get("one").is_some());

        assert_eq!(
            cache.entries.keys().cloned().collect::<Vec<_>>(),
            vec!["two".to_string(), "one".to_string()]
        );
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
