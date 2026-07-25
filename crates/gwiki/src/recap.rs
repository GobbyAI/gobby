//! `gwiki recap`: write the day's session recap page.
//!
//! The session-ingestion payoff: deterministic selection of the day's session
//! digests, one bounded single-shot synthesis (a single completion, never an
//! agent loop, so it survives local models), and a `recaps/YYYY-MM-DD.md`
//! page citing its digests. The rolling "Recent work" block in `_index.md`'s
//! Overview is derived from the `recaps/` directory by catalog regeneration,
//! so it survives every deterministic rebuild and rides the session-start
//! injection.
//!
//! Day attribution is UTC: a session belongs to the target date when its
//! digest's `session_started_at` frontmatter (falling back to the manifest
//! record's `fetched_at`) parses to that UTC calendar day. Timestamps arrive
//! in both `unix-ms:<millis>` (gwiki's `collect_timestamp`) and RFC 3339
//! (transcript adapters) forms. A day with no sessions writes nothing and is
//! not an error.

use std::fs;
use std::path::{Path, PathBuf};

use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;

use crate::explainer::{
    CitationTarget, ExplainerGeneration, ExplainerGenerator, build_explainer_prompt,
    generate_explainer, ground_explainer,
};
use crate::frontmatter::parse_frontmatter;
use crate::sources::{SourceKind, SourceManifest, SourceRecord};
use crate::support::time::{ParsedTimestamp, parse_timestamp};
use crate::synthesis::{ArticleKind, SynthesisInput, SynthesisSource, relative_path, wiki_link};
use crate::{ScopeIdentity, WikiError, catalog, paths};

/// Vault-relative directory holding recap pages.
pub(crate) const RECAPS_DIRECTORY: &str = "recaps";

/// One recap run's outcome, serialized as the command payload.
#[derive(Debug, Clone, Serialize)]
pub struct RecapReport {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub timestamp: String,
    /// Target day (`YYYY-MM-DD`, UTC attribution).
    pub date: String,
    pub sessions_selected: usize,
    /// Selected session source ids, in page listing order.
    pub session_ids: Vec<String>,
    /// Selected digests dropped from the prompt by the token budget; they
    /// still appear in the page's Sessions listing.
    pub sources_truncated: usize,
    /// `generated` / `skipped` / `failed`.
    pub synthesis: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page_path: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page_action: Option<&'static str>,
    pub citations_kept: usize,
    pub citations_stripped: usize,
    pub fallback_sections: usize,
    pub notes: Vec<String>,
}

/// One session digest selected for the target day.
struct SelectedSession {
    record: SourceRecord,
    /// Vault-relative digest path (`knowledge/sources/<id>.md`).
    digest_relative: PathBuf,
    /// Digest body with frontmatter stripped; empty when the digest file is
    /// missing or unreadable.
    body: String,
    title: String,
    /// Best attributed instant, for deterministic chronological ordering.
    instant: DateTime<Utc>,
}

/// Write the recap page for `date` from the day's session digests.
pub fn run(
    vault_root: &Path,
    scope: ScopeIdentity,
    date: NaiveDate,
    generator: Option<ExplainerGenerator<'_>>,
    timestamp: &str,
) -> Result<RecapReport, WikiError> {
    let date_label = date.format("%Y-%m-%d").to_string();
    let mut notes: Vec<String> = Vec::new();
    let mut sessions = select_sessions(vault_root, date, &mut notes)?;
    sessions.sort_by(|left, right| {
        left.instant
            .cmp(&right.instant)
            .then_with(|| left.record.id.cmp(&right.record.id))
    });
    let session_ids: Vec<String> = sessions
        .iter()
        .map(|session| session.record.id.clone())
        .collect();

    let mut report = RecapReport {
        command: "recap",
        scope: scope.clone(),
        timestamp: timestamp.to_string(),
        date: date_label.clone(),
        sessions_selected: sessions.len(),
        session_ids,
        sources_truncated: 0,
        synthesis: "skipped",
        page_path: None,
        page_action: None,
        citations_kept: 0,
        citations_stripped: 0,
        fallback_sections: 0,
        notes,
    };
    // A day with no sessions produces no page, no catalog churn, no log line.
    if sessions.is_empty() {
        return Ok(report);
    }

    let page_relative = PathBuf::from(RECAPS_DIRECTORY).join(format!("{date_label}.md"));
    let page_absolute = vault_root.join(&page_relative);
    let existing_page_body = read_existing_body(&page_absolute)?;
    let page_action = if existing_page_body.is_some() {
        "updated"
    } else {
        "created"
    };

    let input = SynthesisInput {
        handoff_id: format!("recap-{date_label}"),
        topic: format!("Recap of {date_label}"),
        // Empty outline requests a single Overview section from the explainer.
        outline: Vec::new(),
        target_kind: ArticleKind::Topic,
        accepted_sources: sessions
            .iter()
            .map(|session| SynthesisSource {
                title: session.title.clone(),
                path: vault_root.join(&session.digest_relative),
                source_hash: session.record.content_hash.clone(),
                chunks: vec![session.body.clone()],
                existing_page: Some(vault_root.join(&session.digest_relative)),
            })
            .collect(),
        citations: Vec::new(),
        conflicting_claims: Vec::new(),
        missing_evidence: Vec::new(),
        existing_page_body,
        aliases: Vec::new(),
        extra_tags: Vec::new(),
        candidate: false,
    };
    let prompt = build_explainer_prompt(vault_root, &input);
    report.sources_truncated = prompt.truncated_sources;

    let targets: Vec<CitationTarget> = sessions
        .iter()
        .map(|session| {
            let absolute = vault_root.join(&session.digest_relative);
            CitationTarget {
                key: relative_path(vault_root, &absolute),
                link: wiki_link(vault_root, &absolute, &session.title),
                corpus: session.body.clone(),
            }
        })
        .collect();

    let overview = match generate_explainer(&input, &prompt, generator) {
        ExplainerGeneration::Generated { body, .. } => {
            let grounded = ground_explainer(&body, &targets);
            report.synthesis = "generated";
            report.citations_kept = grounded.citations_kept;
            report.citations_stripped = grounded.citations_stripped;
            report.fallback_sections = grounded.fallback_sections;
            grounded.body
        }
        ExplainerGeneration::Failed { error } => {
            report.synthesis = "failed";
            report
                .notes
                .push(format!("recap synthesis failed: {error}"));
            fallback_overview()
        }
        ExplainerGeneration::Skipped => {
            report.synthesis = "skipped";
            fallback_overview()
        }
    };

    let page = render_page(vault_root, &date_label, &overview, &sessions);
    write_page(&page_absolute, &page)?;
    report.page_path = Some(page_relative.clone());
    report.page_action = Some(page_action);

    // Fold the new recap into the deterministic catalog so `_index.md`'s
    // Overview picks up the rolling Recent work block.
    catalog::regenerate(vault_root, &scope)?;
    crate::log::append_logs(
        vault_root,
        None,
        &crate::log::LogEntry {
            timestamp: timestamp.to_string(),
            scope,
            action: crate::log::ACTION_RECAP_COMPLETED.to_string(),
            summary: format!(
                "date={date_label} sessions={} synthesis={} page={page_action}",
                report.sessions_selected, report.synthesis,
            ),
            artifacts: vec![page_relative],
        },
    )?;

    Ok(report)
}

/// Deterministically select the target day's session digests: manifest
/// records of kind `session` whose attributed UTC day equals `date`.
fn select_sessions(
    vault_root: &Path,
    date: NaiveDate,
    notes: &mut Vec<String>,
) -> Result<Vec<SelectedSession>, WikiError> {
    let manifest = SourceManifest::read(vault_root)?;
    let mut selected = Vec::new();
    for record in manifest.entries {
        if record.kind != SourceKind::Session {
            continue;
        }
        let digest_relative = paths::derived_markdown_path(&record)?;
        let digest = read_digest(&vault_root.join(&digest_relative));
        if digest.is_none() {
            notes.push(format!("digest missing for session source {}", record.id));
        }
        let (body, started_at) = digest.unwrap_or_default();
        let Some(instant) = started_at
            .as_deref()
            .and_then(timestamp_for_ordering)
            .or_else(|| timestamp_for_ordering(&record.fetched_at))
        else {
            notes.push(format!(
                "session source {} has no parseable timestamp",
                record.id
            ));
            continue;
        };
        if instant.date_naive() != date {
            continue;
        }
        let title = record
            .title
            .clone()
            .unwrap_or_else(|| format!("Session {}", record.id));
        selected.push(SelectedSession {
            record,
            digest_relative,
            body,
            title,
            instant,
        });
    }
    Ok(selected)
}

/// Parse a digest page into its body and `session_started_at` frontmatter
/// value. Returns `None` when the file is missing or unreadable — the record
/// still recaps from its manifest metadata.
fn read_digest(path: &Path) -> Option<(String, Option<String>)> {
    let markdown = fs::read_to_string(path).ok()?;
    match parse_frontmatter(&markdown) {
        Ok(parsed) => {
            let started_at = parsed
                .metadata
                .unknown
                .get("session_started_at")
                .and_then(|value| value.as_str())
                .map(str::to_string);
            Some((parsed.body.to_string(), started_at))
        }
        Err(_) => Some((markdown, None)),
    }
}

fn timestamp_for_ordering(value: &str) -> Option<DateTime<Utc>> {
    match parse_timestamp(value) {
        ParsedTimestamp::Instant(instant) => Some(instant),
        ParsedTimestamp::DateOnly(date) | ParsedTimestamp::DatePrefix { date, .. } => Some(
            DateTime::from_naive_utc_and_offset(date.and_hms_opt(0, 0, 0)?, Utc),
        ),
        ParsedTimestamp::Unparseable(_) => None,
    }
}

fn fallback_overview() -> String {
    "## Overview\n\nSynthesis was unavailable for this run; the day's sessions are listed below."
        .to_string()
}

fn render_page(
    vault_root: &Path,
    date_label: &str,
    overview: &str,
    sessions: &[SelectedSession],
) -> String {
    let mut page = String::from("---\n");
    page.push_str(&format!("title: \"Recap: {date_label}\"\n"));
    page.push_str("tags:\n  - gwiki\n  - recap\n");
    page.push_str(&format!("recap_date: {date_label}\n"));
    page.push_str("---\n");
    page.push_str(&format!("# Recap: {date_label}\n\n"));
    page.push_str(overview.trim_end());
    page.push_str("\n\n## Sessions\n\n");
    for session in sessions {
        let absolute = vault_root.join(&session.digest_relative);
        page.push_str("- ");
        page.push_str(&wiki_link(vault_root, &absolute, &session.title));
        page.push('\n');
    }
    crate::page_version::stamp_generated_page(
        &page,
        sessions
            .iter()
            .map(|session| session.record.content_hash.as_str()),
    )
}

fn write_page(path: &Path, content: &str) -> Result<(), WikiError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create recaps directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    fs::write(path, content).map_err(|error| WikiError::Io {
        action: "write recap page",
        path: Some(path.to_path_buf()),
        source: error,
    })
}

fn read_existing_body(path: &Path) -> Result<Option<String>, WikiError> {
    let markdown = match fs::read_to_string(path) {
        Ok(markdown) => markdown,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read existing recap page",
                path: Some(path.to_path_buf()),
                source: error,
            });
        }
    };
    let body = match parse_frontmatter(&markdown) {
        Ok(parsed) => parsed.body.to_string(),
        Err(_) => markdown,
    };
    Ok(Some(body))
}

/// Human-readable summary of one recap run.
pub fn render_text(report: &RecapReport) -> String {
    let mut text = format!(
        "recap {}: {} session(s), synthesis {}",
        report.date, report.sessions_selected, report.synthesis
    );
    match (&report.page_path, report.page_action) {
        (Some(path), Some(action)) => {
            text.push_str(&format!(", {action} {}", path.display()));
        }
        _ => text.push_str(", no page written"),
    }
    text.push('\n');
    for note in &report.notes {
        text.push_str("note: ");
        text.push_str(note);
        text.push('\n');
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::explainer::{ExplainerPrompt, ExplainerResponse};
    use crate::sources::{CompileStatus, IngestionMethod};

    /// 2026-07-04T04:13:20Z.
    const TIMESTAMP: &str = "unix-ms:1783138400000";

    fn scope() -> ScopeIdentity {
        ScopeIdentity::topic("recap-test")
    }

    fn target_date() -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 7, 4).expect("valid date")
    }

    fn session_record(id: &str, fetched_at: &str) -> SourceRecord {
        SourceRecord {
            id: id.to_string(),
            location: format!("session:{id}"),
            canonical_location: format!("session:{id}"),
            kind: SourceKind::Session,
            fetched_at: fetched_at.to_string(),
            last_verified_at: fetched_at.to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
            content_hash: format!("{id}-hash"),
            title: Some(format!("Session {id}")),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
            replay: None,
        }
    }

    fn write_file(root: &Path, relative: &str, content: &str) {
        let path = root.join(relative);
        fs::create_dir_all(path.parent().expect("parent")).expect("create parent");
        fs::write(path, content).expect("write file");
    }

    /// Register a session source with a digest page. `started_at: None` omits
    /// the `session_started_at` frontmatter so attribution falls back to the
    /// record's `fetched_at`.
    fn seed_session(root: &Path, id: &str, fetched_at: &str, started_at: Option<&str>, body: &str) {
        let record = session_record(id, fetched_at);
        let mut digest = String::from("---\n");
        digest.push_str(&format!("title: \"Session {id}\"\n"));
        if let Some(started_at) = started_at {
            digest.push_str(&format!("session_started_at: \"{started_at}\"\n"));
        }
        digest.push_str("---\n");
        digest.push_str(body);
        write_file(root, &format!("knowledge/sources/{id}.md"), &digest);
        SourceManifest::update(root, |manifest| {
            manifest.entries.push(record.clone());
            Ok(true)
        })
        .expect("seed manifest entry");
    }

    fn cited_generator(
        citation: &'static str,
    ) -> impl FnMut(&ExplainerPrompt) -> Result<ExplainerResponse, String> {
        move |_prompt: &ExplainerPrompt| {
            Ok(ExplainerResponse {
                text: format!("## Overview\n\nShipped the recap feature [source: {citation}]."),
                model: Some("test-model".to_string()),
                route: "test",
                tool_use_count: None,
                turns: None,
                usage: None,
            })
        }
    }

    #[test]
    fn recap_writes_cited_page_recent_work_block_and_log() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_session(
            root,
            "s-late",
            TIMESTAMP,
            Some("2026-07-04T15:00:00Z"),
            "Later work on the recap page.\n",
        );
        seed_session(
            root,
            "s-early",
            TIMESTAMP,
            Some("2026-07-04T09:00:00Z"),
            "Morning work on gwiki.\n",
        );
        seed_session(
            root,
            "s-other-day",
            TIMESTAMP,
            Some("2026-07-03T12:00:00Z"),
            "Yesterday's work.\n",
        );
        // Same-day non-session sources never join a recap.
        SourceManifest::update(root, |manifest| {
            manifest.entries.push(SourceRecord {
                kind: SourceKind::Markdown,
                ..session_record("not-a-session", "2026-07-04T10:00:00Z")
            });
            Ok(true)
        })
        .expect("seed non-session entry");

        let mut generate = cited_generator("knowledge/sources/s-early.md");
        let report =
            run(root, scope(), target_date(), Some(&mut generate), TIMESTAMP).expect("recap run");

        assert_eq!(report.sessions_selected, 2);
        assert_eq!(report.session_ids, vec!["s-early", "s-late"]);
        assert_eq!(report.synthesis, "generated");
        assert_eq!(report.page_action, Some("created"));
        assert_eq!(report.citations_kept, 1);
        assert_eq!(
            report.page_path.as_deref(),
            Some(Path::new("recaps/2026-07-04.md"))
        );

        let page =
            fs::read_to_string(root.join("recaps/2026-07-04.md")).expect("recap page written");
        assert!(page.contains("title: \"Recap: 2026-07-04\""), "{page}");
        assert!(page.contains("  - recap\n"), "recap tag rendered: {page}");
        assert!(page.contains(&format!(
            "content_hash: {}",
            crate::page_version::content_hash(&page)
        )));
        assert!(
            page.contains("compiled_from:\n  - s-early-hash\n  - s-late-hash\n"),
            "{page}"
        );
        assert!(
            page.contains("[[knowledge/sources/s-early|Session s-early]]"),
            "citation grounded to the digest wiki link: {page}"
        );
        assert!(page.contains("## Sessions"), "{page}");
        assert!(
            page.contains("[[knowledge/sources/s-late|Session s-late]]"),
            "all selected digests listed: {page}"
        );
        assert!(
            !page.contains("s-other-day") && !page.contains("not-a-session"),
            "other days and non-session sources excluded: {page}"
        );

        let index = fs::read_to_string(root.join("_index.md")).expect("index regenerated");
        assert!(
            index.contains("Recent work: [[recaps/2026-07-04|2026-07-04]]"),
            "recent work block folded into the Overview: {index}"
        );

        let log = fs::read_to_string(root.join("log.md")).expect("log written");
        assert!(log.contains("recap_completed:"), "{log}");
    }

    #[test]
    fn recap_day_with_no_sessions_writes_nothing() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_session(
            root,
            "s-other-day",
            TIMESTAMP,
            Some("2026-07-03T12:00:00Z"),
            "Yesterday's work.\n",
        );

        let report = run(root, scope(), target_date(), None, TIMESTAMP).expect("recap run");

        assert_eq!(report.sessions_selected, 0);
        assert!(report.page_path.is_none());
        assert!(report.page_action.is_none());
        assert!(!root.join(RECAPS_DIRECTORY).exists(), "no recaps directory");
        assert!(!root.join("log.md").exists(), "no log line for a no-op day");
        assert!(!root.join("_index.md").exists(), "no catalog churn");
    }

    #[test]
    fn recap_selection_prefers_started_at_and_falls_back_to_fetched_at() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        // In-day started_at wins over an out-of-day fetched_at.
        seed_session(
            root,
            "s-started-in-day",
            "unix-ms:1751000000000",
            Some("2026-07-04T08:00:00Z"),
            "Work.\n",
        );
        // No started_at: the in-day unix-ms fetched_at attributes the day.
        seed_session(root, "s-fetched-in-day", TIMESTAMP, None, "Work.\n");
        // Out-of-day started_at excludes even with an in-day fetched_at.
        seed_session(
            root,
            "s-started-other-day",
            TIMESTAMP,
            Some("2026-07-03T23:00:00Z"),
            "Work.\n",
        );

        let report = run(root, scope(), target_date(), None, TIMESTAMP).expect("recap run");

        assert_eq!(
            report.session_ids,
            vec!["s-fetched-in-day", "s-started-in-day"],
            "selection is by attributed day, ordered chronologically"
        );
    }

    #[test]
    fn recap_rerun_updates_existing_page_and_carries_its_body() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_session(
            root,
            "s-a",
            TIMESTAMP,
            Some("2026-07-04T09:00:00Z"),
            "Work.\n",
        );

        let mut generate = cited_generator("knowledge/sources/s-a.md");
        let first =
            run(root, scope(), target_date(), Some(&mut generate), TIMESTAMP).expect("first run");
        assert_eq!(first.page_action, Some("created"));

        let mut seen_existing_body = false;
        let mut second_generate = |prompt: &ExplainerPrompt| {
            seen_existing_body = prompt.user.contains("Current page content");
            Ok(ExplainerResponse {
                text: "## Overview\n\nUpdated [source: knowledge/sources/s-a.md].".to_string(),
                model: None,
                route: "test",
                tool_use_count: None,
                turns: None,
                usage: None,
            })
        };
        let second = run(
            root,
            scope(),
            target_date(),
            Some(&mut second_generate),
            TIMESTAMP,
        )
        .expect("second run");

        assert_eq!(second.page_action, Some("updated"));
        assert!(
            seen_existing_body,
            "rerun folds the existing page body into the prompt"
        );
    }

    #[test]
    fn recap_synthesis_failure_still_writes_deterministic_listing() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_session(
            root,
            "s-a",
            TIMESTAMP,
            Some("2026-07-04T09:00:00Z"),
            "Work.\n",
        );

        let mut generate =
            |_prompt: &ExplainerPrompt| Err::<ExplainerResponse, _>("model exploded".to_string());
        let report =
            run(root, scope(), target_date(), Some(&mut generate), TIMESTAMP).expect("recap run");

        assert_eq!(report.synthesis, "failed");
        assert!(
            report
                .notes
                .iter()
                .any(|note| note.contains("model exploded")),
            "failure recorded: {:?}",
            report.notes
        );
        let page =
            fs::read_to_string(root.join("recaps/2026-07-04.md")).expect("page still written");
        assert!(page.contains("Synthesis was unavailable"), "{page}");
        assert!(
            page.contains("[[knowledge/sources/s-a|Session s-a]]"),
            "deterministic listing survives: {page}"
        );
    }

    #[test]
    fn recap_skipped_synthesis_writes_deterministic_page() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_session(
            root,
            "s-a",
            TIMESTAMP,
            Some("2026-07-04T09:00:00Z"),
            "Work.\n",
        );

        let report = run(root, scope(), target_date(), None, TIMESTAMP).expect("recap run");

        assert_eq!(report.synthesis, "skipped");
        assert_eq!(report.page_action, Some("created"));
        let page = fs::read_to_string(root.join("recaps/2026-07-04.md")).expect("page written");
        assert!(page.contains("## Sessions"), "{page}");
    }

    #[test]
    fn timestamp_for_ordering_handles_unix_ms_rfc3339_and_date_prefixes() {
        assert_eq!(
            timestamp_for_ordering(TIMESTAMP)
                .expect("unix-ms parses")
                .date_naive(),
            target_date()
        );
        assert_eq!(
            timestamp_for_ordering("2026-07-04T23:59:59-01:00")
                .expect("rfc3339 parses")
                .date_naive(),
            NaiveDate::from_ymd_opt(2026, 7, 5).expect("valid date"),
            "offsets normalize to UTC"
        );
        assert_eq!(
            timestamp_for_ordering("2026-07-04 (approximate)")
                .expect("bare date prefix parses")
                .date_naive(),
            target_date()
        );
        assert!(timestamp_for_ordering("unix-ms:not-a-number").is_none());
        assert!(timestamp_for_ordering("last tuesday").is_none());
        assert!(timestamp_for_ordering("").is_none());
    }
}
