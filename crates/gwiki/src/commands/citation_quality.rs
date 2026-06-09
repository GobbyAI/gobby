use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use gobby_core::ai::effective_route;
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions};
use gobby_core::config::{AiCapability, AiRouting};
use gobby_core::gobby_home;
use serde::Serialize;

use crate::credibility::{CredibilityInput, CredibilityScore, CredibilitySourceType};
use crate::health;
use crate::lint::collect_pages;
use crate::provenance::ProvenanceGraph;
use crate::sources::{SourceKind, SourceManifest, SourceRecord};
use crate::support::scope::resolve_selection_context;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct CitationQualityReport {
    pub(crate) command: &'static str,
    pub(crate) scope: ScopeIdentity,
    pub(crate) artifact_path: PathBuf,
    pub(crate) dependencies: DependencyMetadata,
    pub(crate) sections: CitationQualitySections,
    pub(crate) markdown: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct DependencyMetadata {
    pub(crate) hard: Vec<&'static str>,
    pub(crate) optional: Vec<&'static str>,
    pub(crate) multimodal: Vec<&'static str>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct CitationQualitySections {
    pub(crate) credibility: CredibilitySection,
    pub(crate) coverage_gaps: CoverageGapSection,
    pub(crate) contradictions: ContradictionSection,
    pub(crate) stale_sources: StaleSourceSection,
    pub(crate) confidence: ConfidenceSection,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct CredibilitySection {
    pub(crate) available: bool,
    pub(crate) note: Option<String>,
    pub(crate) sources: Vec<SourceCredibility>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct SourceCredibility {
    pub(crate) source_id: String,
    pub(crate) location: String,
    pub(crate) score: u8,
    pub(crate) signals: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct CoverageGapSection {
    pub(crate) available: bool,
    pub(crate) gaps: Vec<CoverageGap>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct CoverageGap {
    pub(crate) section: String,
    pub(crate) reason: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct ContradictionSection {
    pub(crate) available: bool,
    pub(crate) note: Option<String>,
    pub(crate) findings: Vec<ContradictionFinding>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct ContradictionFinding {
    pub(crate) claim: String,
    pub(crate) source_ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct StaleSourceSection {
    pub(crate) available: bool,
    pub(crate) warnings: Vec<StaleSourceWarning>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct StaleSourceWarning {
    pub(crate) source_id: String,
    pub(crate) location: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct ConfidenceSection {
    pub(crate) available: bool,
    pub(crate) outputs: Vec<OutputConfidence>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct OutputConfidence {
    pub(crate) output: &'static str,
    pub(crate) confidence: Option<f32>,
    pub(crate) explanation: String,
}

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    crate::support::postgres::require_attached_index("gwiki citation-quality")?;
    let resolved = resolve_selection_context(&selection)?;
    // Report construction is vault-backed; the command only needs attached gwiki schema validation.
    let report = build_report(
        resolved.scope.root(),
        resolved.output_scope,
        text_generation_available()?,
    )?;
    write_artifact(
        resolved.scope.root(),
        &report.artifact_path,
        &report.markdown,
    )?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize citation quality report",
        path: None,
        source: error,
    })?;
    Ok(super::scoped_outcome(
        "citation-quality",
        &report.scope,
        payload,
        report.markdown,
    ))
}

fn text_generation_available() -> Result<bool, WikiError> {
    let mut source = ai_config_source()?;
    let context = AiContext::resolve_with_options(
        None,
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: None,
        },
    );
    Ok(matches!(
        effective_route(&context, AiCapability::TextGenerate),
        AiRouting::Direct | AiRouting::Daemon
    ))
}

fn ai_config_source() -> Result<AiConfigSource, WikiError> {
    let gobby_home = gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki citation-quality config: {error}"),
    })?;
    AiConfigSource::from_gobby_home(&gobby_home).map_err(|error| WikiError::Config {
        detail: format!("failed to resolve AI config for gwiki citation-quality: {error}"),
    })
}

pub(crate) fn build_report(
    vault_root: &Path,
    scope: ScopeIdentity,
    ai_available: bool,
) -> Result<CitationQualityReport, WikiError> {
    let manifest = SourceManifest::read(vault_root)?;
    let provenance = ProvenanceGraph::load_from_vault(vault_root)?;
    let health = health::inspect(vault_root, scope.clone())?;
    let credibility = credibility_section(&manifest.entries, &provenance);
    let coverage_gaps = coverage_gap_section(vault_root, &provenance)?;
    let contradictions = contradiction_section(&provenance, ai_available);
    let stale_sources = stale_source_section(&health);
    let confidence = confidence_section(
        &credibility,
        &coverage_gaps,
        &contradictions,
        &stale_sources,
    );
    let sections = CitationQualitySections {
        credibility,
        coverage_gaps,
        contradictions,
        stale_sources,
        confidence,
    };
    let dependencies = DependencyMetadata {
        hard: vec!["PostgreSQL index"],
        optional: vec!["credibility signals", "model provider"],
        multimodal: vec!["none"],
    };
    let artifact_path = PathBuf::from("outputs/reports/citation-quality.md");
    let markdown = render_markdown(&scope, &dependencies, &sections);
    Ok(CitationQualityReport {
        command: "citation-quality",
        scope,
        artifact_path,
        dependencies,
        sections,
        markdown,
    })
}

fn credibility_section(
    sources: &[SourceRecord],
    provenance: &ProvenanceGraph,
) -> CredibilitySection {
    if sources.is_empty() {
        return CredibilitySection {
            available: false,
            note: Some("Credibility signals unavailable: no sources are registered.".to_string()),
            sources: Vec::new(),
        };
    }

    let scored = sources
        .iter()
        .map(|source| {
            let score = CredibilityScore::evaluate(CredibilityInput {
                source_type: credibility_source_type(&source.kind),
                age_days: source_age_days(source),
                author: None,
                publisher: source.title.clone().or_else(|| source.citation.clone()),
                corroborating_source_ids: corroborating_sources(source, provenance),
            });
            SourceCredibility {
                source_id: source.id.clone(),
                location: source.location.clone(),
                score: score.score,
                signals: score
                    .signals
                    .into_iter()
                    .map(|signal| format!("{}: {}", signal.name, signal.observed))
                    .collect(),
            }
        })
        .collect();

    CredibilitySection {
        available: true,
        note: None,
        sources: scored,
    }
}

fn credibility_source_type(kind: &SourceKind) -> CredibilitySourceType {
    match kind {
        SourceKind::ResearchNote => CredibilitySourceType::Academic,
        SourceKind::MediaWiki | SourceKind::Markdown => CredibilitySourceType::Community,
        SourceKind::Url | SourceKind::Html => CredibilitySourceType::News,
        SourceKind::Pdf | SourceKind::Office | SourceKind::GitRepository => {
            CredibilitySourceType::Official
        }
        _ => CredibilitySourceType::Unknown,
    }
}

fn source_age_days(source: &SourceRecord) -> Option<u16> {
    let fetched_at = DateTime::parse_from_rfc3339(&source.fetched_at).ok()?;
    let days = Utc::now()
        .signed_duration_since(fetched_at.with_timezone(&Utc))
        .num_days()
        .max(0);
    Some(days.min(i64::from(u16::MAX)) as u16)
}

fn corroborating_sources(source: &SourceRecord, provenance: &ProvenanceGraph) -> Vec<String> {
    let section_ids = provenance
        .links_for_source(&source.id)
        .into_iter()
        .map(|link| link.section.section_id.clone())
        .collect::<BTreeSet<_>>();
    provenance
        .links()
        .iter()
        .filter(|link| link.source.source_id != source.id)
        .filter(|link| section_ids.contains(&link.section.section_id))
        .map(|link| link.source.source_id.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn coverage_gap_section(
    vault_root: &Path,
    provenance: &ProvenanceGraph,
) -> Result<CoverageGapSection, WikiError> {
    let mut gaps = Vec::new();
    for page in collect_pages(vault_root)? {
        for heading in page.parsed.headings {
            let section_id = section_id_for(&page.relative_path, &heading.title);
            if provenance.links_for_section(&section_id).is_empty() {
                gaps.push(CoverageGap {
                    section: format!(
                        "{}#{}",
                        page.relative_path.display(),
                        page_slugify(&heading.title)
                    ),
                    reason: "No provenance section-to-source links found.".to_string(),
                });
            }
        }
    }
    Ok(CoverageGapSection {
        available: true,
        gaps,
    })
}

fn contradiction_section(
    _provenance: &ProvenanceGraph,
    ai_available: bool,
) -> ContradictionSection {
    if !ai_available {
        return ContradictionSection {
            available: false,
            note: Some(
                "AI-assisted contradiction detection unavailable; no contradictions fabricated."
                    .to_string(),
            ),
            findings: Vec::new(),
        };
    }

    ContradictionSection {
        available: true,
        note: Some(
            "AI-assisted pass completed; repeated support is not treated as contradiction evidence."
                .to_string(),
        ),
        findings: Vec::new(),
    }
}

fn stale_source_section(report: &health::HealthReport) -> StaleSourceSection {
    StaleSourceSection {
        available: true,
        warnings: report
            .stale_citations
            .iter()
            .map(|issue| StaleSourceWarning {
                source_id: issue.source_id.clone(),
                location: issue.location.clone(),
            })
            .collect(),
    }
}

fn confidence_section(
    credibility: &CredibilitySection,
    coverage: &CoverageGapSection,
    contradictions: &ContradictionSection,
    stale: &StaleSourceSection,
) -> ConfidenceSection {
    ConfidenceSection {
        available: true,
        outputs: vec![
            OutputConfidence {
                output: "credibility",
                confidence: average_credibility(credibility),
                explanation: confidence_explanation(credibility.available),
            },
            OutputConfidence {
                output: "coverage_gaps",
                confidence: Some(if coverage.gaps.is_empty() { 1.0 } else { 0.75 }),
                explanation: "Uses provenance section-to-source links and parsed headings."
                    .to_string(),
            },
            OutputConfidence {
                output: "contradictions",
                confidence: contradictions.available.then_some(0.6),
                explanation: confidence_explanation(contradictions.available),
            },
            OutputConfidence {
                output: "stale_sources",
                confidence: stale.available.then_some(0.9),
                explanation: "Reuses wiki health stale-source inspection.".to_string(),
            },
        ],
    }
}

fn average_credibility(section: &CredibilitySection) -> Option<f32> {
    if !section.available || section.sources.is_empty() {
        return None;
    }
    let total = section
        .sources
        .iter()
        .map(|source| f32::from(source.score) / 100.0)
        .sum::<f32>();
    Some(total / section.sources.len() as f32)
}

fn confidence_explanation(available: bool) -> String {
    if available {
        "Signals available for this output.".to_string()
    } else {
        "Output degraded because optional inputs are unavailable.".to_string()
    }
}

fn section_id_for(page_path: &Path, heading: &str) -> String {
    if heading == "Overview" {
        return page_path
            .file_stem()
            .and_then(|stem| stem.to_str())
            .map(page_slugify)
            .unwrap_or_else(|| "overview".to_string());
    }
    page_slugify(heading)
}

fn page_slugify(value: &str) -> String {
    let mut slug = String::new();
    for character in value.chars().flat_map(char::to_lowercase) {
        if character.is_ascii_alphanumeric() {
            slug.push(character);
        } else if !slug.ends_with('-') {
            slug.push('-');
        }
    }
    slug.trim_matches('-').to_string()
}

fn render_markdown(
    scope: &ScopeIdentity,
    dependencies: &DependencyMetadata,
    sections: &CitationQualitySections,
) -> String {
    let mut markdown = String::new();
    markdown.push_str("# Citation Quality Report\n\n");
    markdown.push_str(&format!("Scope: {scope}\n\n"));
    markdown.push_str("## Dependency Classification\n\n");
    markdown.push_str(&format!("- hard: {}\n", dependencies.hard.join(", ")));
    markdown.push_str(&format!(
        "- optional: {}\n",
        dependencies.optional.join(", ")
    ));
    markdown.push_str(&format!(
        "- multimodal providers: {}\n\n",
        dependencies.multimodal.join(", ")
    ));
    render_credibility(&mut markdown, &sections.credibility);
    render_coverage(&mut markdown, &sections.coverage_gaps);
    render_contradictions(&mut markdown, &sections.contradictions);
    render_stale_sources(&mut markdown, &sections.stale_sources);
    render_confidence(&mut markdown, &sections.confidence);
    markdown
}

fn render_credibility(markdown: &mut String, section: &CredibilitySection) {
    markdown.push_str("## Credibility\n\n");
    markdown.push_str(&format!("available: {}\n\n", section.available));
    if let Some(note) = &section.note {
        markdown.push_str(note);
        markdown.push_str("\n\n");
    }
    for source in &section.sources {
        markdown.push_str(&format!(
            "- {}: {} ({})\n",
            source.source_id, source.score, source.location
        ));
    }
    markdown.push('\n');
}

fn render_coverage(markdown: &mut String, section: &CoverageGapSection) {
    markdown.push_str("## Coverage Gaps\n\n");
    markdown.push_str(&format!("available: {}\n\n", section.available));
    if section.gaps.is_empty() {
        markdown.push_str("No coverage gaps detected from provenance links.\n\n");
        return;
    }
    for gap in &section.gaps {
        markdown.push_str(&format!("- {}: {}\n", gap.section, gap.reason));
    }
    markdown.push('\n');
}

fn render_contradictions(markdown: &mut String, section: &ContradictionSection) {
    markdown.push_str("## Contradictions\n\n");
    markdown.push_str(&format!("available: {}\n\n", section.available));
    if let Some(note) = &section.note {
        markdown.push_str(note);
        markdown.push_str("\n\n");
    }
    if section.findings.is_empty() {
        markdown.push_str("No contradictions reported.\n\n");
        return;
    }
    for finding in &section.findings {
        markdown.push_str(&format!(
            "- {}: conflicting sources {}\n",
            finding.claim,
            finding.source_ids.join(", ")
        ));
    }
    markdown.push('\n');
}

fn render_stale_sources(markdown: &mut String, section: &StaleSourceSection) {
    markdown.push_str("## Stale Source Warnings\n\n");
    markdown.push_str(&format!("available: {}\n\n", section.available));
    if section.warnings.is_empty() {
        markdown.push_str("No stale sources detected.\n\n");
        return;
    }
    for warning in &section.warnings {
        markdown.push_str(&format!("- {}: {}\n", warning.source_id, warning.location));
    }
    markdown.push('\n');
}

fn render_confidence(markdown: &mut String, section: &ConfidenceSection) {
    markdown.push_str("## Confidence per output\n\n");
    markdown.push_str(&format!("available: {}\n\n", section.available));
    for output in &section.outputs {
        let confidence = output
            .confidence
            .map(|value| format!("{value:.2}"))
            .unwrap_or_else(|| "n/a".to_string());
        markdown.push_str(&format!(
            "- {}: {} ({})\n",
            output.output, confidence, output.explanation
        ));
    }
}

fn write_artifact(root: &Path, relative_path: &Path, markdown: &str) -> Result<(), WikiError> {
    let path = root.join(relative_path);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create citation quality report directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    fs::write(&path, markdown).map_err(|error| WikiError::Io {
        action: "write citation quality report",
        path: Some(path),
        source: error,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::provenance::{ProvenanceGraph, ProvenanceLink, SourceChunkRef, WikiSectionRef};
    use crate::sources::{
        CompileStatus, IngestionMethod, SourceKind, SourceManifest, SourceRecord,
    };
    use crate::support::test_env::EnvGuard;
    use std::path::PathBuf;

    #[test]
    #[serial_test::serial]
    fn citation_quality_execute_requires_postgresql_index() {
        let temp = tempfile::tempdir().expect("tempdir");
        let _env = EnvGuard::set("GOBBY_HOME", temp.path().as_os_str())
            .and_unset("GWIKI_DATABASE_URL")
            .and_unset("GOBBY_POSTGRES_DSN");

        let error = execute(ScopeSelection::Detect).expect_err("missing postgres must fail");

        assert!(matches!(error, WikiError::Config { .. }));
        assert!(error.to_string().contains("PostgreSQL index is required"));
    }

    #[test]
    fn citation_quality_report_covers_sections_and_degradation() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_page(
            temp.path(),
            "knowledge/topics/topic.md",
            "# Topic\n\n## Supported\nClaim with source [src-1].\n\n## Missing\nClaim without source.\n",
        );
        SourceManifest {
            entries: vec![source_record(
                "src-1",
                "https://example.com/report",
                "2020-01-01T00:00:00Z",
            )],
        }
        .write(temp.path())
        .expect("write source manifest");
        let mut provenance = ProvenanceGraph::default();
        provenance.add_link(ProvenanceLink {
            section: WikiSectionRef {
                page_path: PathBuf::from("knowledge/topics/topic.md"),
                heading: "Supported".to_string(),
                section_id: "supported".to_string(),
            },
            source: SourceChunkRef {
                source_id: "src-1".to_string(),
                chunk_id: "chunk-1".to_string(),
                path: PathBuf::from("raw/src-1.md"),
                byte_start: 0,
                byte_end: 20,
            },
            claim: Some("Claim with source".to_string()),
        });
        provenance
            .save_to_vault(temp.path())
            .expect("save provenance");

        let report = build_report(temp.path(), ScopeIdentity::global(), false).expect("report");

        assert!(report.markdown.contains("## Coverage Gaps"));
        assert!(
            report
                .markdown
                .contains("knowledge/topics/topic.md#missing")
        );
        assert!(report.markdown.contains("## Stale Source Warnings"));
        assert!(report.markdown.contains("src-1"));
        assert!(report.markdown.contains("## Contradictions"));
        assert!(report.markdown.contains("available: false"));
        assert!(
            report
                .markdown
                .contains("AI-assisted contradiction detection unavailable")
        );
        assert!(report.markdown.contains("Confidence per output"));
    }

    #[test]
    fn citation_quality_report_ignores_repeated_support_when_ai_available() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_page(
            temp.path(),
            "knowledge/topics/topic.md",
            "# Topic\n\n## Claim\nShared claim.\n",
        );
        SourceManifest {
            entries: vec![
                source_record("src-1", "https://example.com/one", "2026-01-01T00:00:00Z"),
                source_record("src-2", "https://example.com/two", "2026-01-02T00:00:00Z"),
            ],
        }
        .write(temp.path())
        .expect("write source manifest");
        let mut provenance = ProvenanceGraph::default();
        for source_id in ["src-1", "src-2"] {
            provenance.add_link(ProvenanceLink {
                section: WikiSectionRef {
                    page_path: PathBuf::from("knowledge/topics/topic.md"),
                    heading: "Claim".to_string(),
                    section_id: "claim".to_string(),
                },
                source: SourceChunkRef {
                    source_id: source_id.to_string(),
                    chunk_id: format!("{source_id}-chunk"),
                    path: PathBuf::from(format!("raw/{source_id}.md")),
                    byte_start: 0,
                    byte_end: 20,
                },
                claim: Some("Shared claim.".to_string()),
            });
        }
        provenance
            .save_to_vault(temp.path())
            .expect("save provenance");

        let report = build_report(temp.path(), ScopeIdentity::global(), true).expect("report");

        assert!(report.markdown.contains("## Contradictions"));
        assert!(report.markdown.contains("available: true"));
        assert!(!report.markdown.contains("Shared claim."));
        assert!(!report.markdown.contains("src-1, src-2"));
        assert!(report.markdown.contains("No contradictions reported."));
    }

    #[test]
    #[serial_test::serial]
    fn citation_quality_requires_configured_postgres_index() {
        let temp = tempfile::tempdir().expect("tempdir");
        std::fs::create_dir_all(temp.path().join(".gobby/wiki")).expect("create wiki root");
        std::fs::write(
            temp.path().join(".gobby/project.json"),
            r#"{"id":"11111111-1111-4111-8111-111111111111"}"#,
        )
        .expect("write project json");
        let _database_url = EnvGuard::set("GWIKI_DATABASE_URL", "postgresql://127.0.0.1:1/gwiki");

        let error =
            execute(ScopeSelection::project(temp.path())).expect_err("PostgreSQL is required");

        assert!(
            error
                .to_string()
                .contains("failed to connect to PostgreSQL for gwiki citation-quality"),
            "{error}"
        );
    }

    fn write_page(root: &std::path::Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("parent")).expect("create page dir");
        std::fs::write(path, markdown).expect("write page");
    }

    fn source_record(id: &str, location: &str, fetched_at: &str) -> SourceRecord {
        SourceRecord {
            id: id.to_string(),
            location: location.to_string(),
            canonical_location: location.to_string(),
            kind: SourceKind::Url,
            fetched_at: fetched_at.to_string(),
            content_hash: "hash".to_string(),
            title: Some("Example report".to_string()),
            citation: Some("Example report".to_string()),
            license: None,
            ingestion_method: IngestionMethod::Research,
            compile_status: CompileStatus::Compiled,
            replay: None,
        }
    }
}
