//! Explainable credibility scoring for raw wiki sources, and the derived
//! page-level confidence composed from it.

use std::collections::BTreeSet;
use std::path::Path;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::provenance::ProvenanceGraph;
use crate::sources::{SourceKind, SourceRecord};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CredibilitySourceType {
    Official,
    Academic,
    News,
    Community,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CredibilityInput {
    pub source_type: CredibilitySourceType,
    pub age_days: Option<u16>,
    pub author: Option<String>,
    pub publisher: Option<String>,
    pub corroborating_source_ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CredibilitySignal {
    pub name: String,
    pub observed: String,
    pub explanation: String,
    pub weight: i16,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CredibilityScore {
    pub score: u8,
    pub signals: Vec<CredibilitySignal>,
}

impl CredibilityScore {
    pub fn evaluate(input: CredibilityInput) -> Self {
        let mut raw_score: i16 = 50;
        let signals = vec![
            source_type_signal(input.source_type),
            freshness_signal(input.age_days),
            author_signal(input.author.as_deref()),
            publisher_signal(input.publisher.as_deref()),
            corroboration_signal(input.corroborating_source_ids.len()),
        ];

        for signal in &signals {
            raw_score += signal.weight;
        }

        Self {
            score: raw_score.clamp(0, 100) as u8,
            signals,
        }
    }
}

fn source_type_signal(source_type: CredibilitySourceType) -> CredibilitySignal {
    let (observed, weight, explanation) = match source_type {
        CredibilitySourceType::Official => (
            "official",
            25,
            "Official or primary publishers receive the strongest source-type boost.",
        ),
        CredibilitySourceType::Academic => (
            "academic",
            20,
            "Academic sources are usually reviewed or institutionally accountable.",
        ),
        CredibilitySourceType::News => (
            "news",
            10,
            "News sources add moderate confidence when paired with corroboration.",
        ),
        CredibilitySourceType::Community => (
            "community",
            0,
            "Community sources need stronger corroboration from independent records.",
        ),
        CredibilitySourceType::Unknown => (
            "unknown",
            -10,
            "Unknown source type lowers confidence until metadata is added.",
        ),
    };
    signal("source_type", observed, weight, explanation)
}

fn freshness_signal(age_days: Option<u16>) -> CredibilitySignal {
    match age_days {
        Some(days) if days <= 30 => signal(
            "freshness",
            format!("{days} days old"),
            15,
            "Fresh sources are preferred for claims likely to change.",
        ),
        Some(days) if days <= 365 => signal(
            "freshness",
            format!("{days} days old"),
            5,
            "Moderately recent sources retain some freshness confidence.",
        ),
        Some(days) => signal(
            "freshness",
            format!("{days} days old"),
            -10,
            "Older sources need corroboration before synthesis.",
        ),
        None => signal(
            "freshness",
            "unknown",
            -5,
            "Missing freshness metadata lowers confidence.",
        ),
    }
}

fn author_signal(author: Option<&str>) -> CredibilitySignal {
    match author.filter(|author| !author.trim().is_empty()) {
        Some(author) => signal(
            "author",
            author,
            5,
            "Named authors improve accountability and citation quality.",
        ),
        None => signal(
            "author",
            "missing",
            -5,
            "Missing author metadata limits accountability.",
        ),
    }
}

fn publisher_signal(publisher: Option<&str>) -> CredibilitySignal {
    match publisher.filter(|publisher| !publisher.trim().is_empty()) {
        Some(publisher) => signal(
            "publisher",
            publisher,
            5,
            "Named publishers make provenance easier to audit.",
        ),
        None => signal(
            "publisher",
            "missing",
            -5,
            "Missing publisher metadata weakens auditability.",
        ),
    }
}

fn corroboration_signal(count: usize) -> CredibilitySignal {
    match count {
        0 => signal(
            "corroboration",
            "0 sources",
            -5,
            "Uncorroborated claims should remain lower confidence.",
        ),
        1 => signal(
            "corroboration",
            "1 source",
            5,
            "One corroborating source adds limited support.",
        ),
        count => signal(
            "corroboration",
            format!("{count} sources"),
            10,
            "Multiple corroborating sources improve synthesis confidence.",
        ),
    }
}

fn signal(
    name: impl Into<String>,
    observed: impl Into<String>,
    weight: i16,
    explanation: impl Into<String>,
) -> CredibilitySignal {
    CredibilitySignal {
        name: name.into(),
        observed: observed.into(),
        explanation: explanation.into(),
        weight,
    }
}

/// Build the credibility input for a registered source. Shared by the
/// citation-quality report and page-confidence composition so both surfaces
/// score a source identically.
pub(crate) fn credibility_input_for_source(
    source: &SourceRecord,
    provenance: &ProvenanceGraph,
) -> CredibilityInput {
    CredibilityInput {
        source_type: credibility_source_type(&source.kind),
        age_days: source_age_days(source),
        author: None,
        publisher: source.title.clone().or_else(|| source.citation.clone()),
        corroborating_source_ids: corroborating_sources(source, provenance),
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

/// Other sources cited by the sections this source informs — corroboration in
/// the provenance graph rather than in page prose.
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

/// Freshness half-life (days) by content class: derived surfaces that track
/// live activity decay fast; curated synthesis stays trustworthy far longer.
pub(crate) fn half_life_days_for_content(relative_path: &Path) -> f64 {
    if relative_path.starts_with("knowledge/sources") || relative_path.starts_with("recaps") {
        14.0
    } else if relative_path.starts_with("knowledge/concepts")
        || relative_path.starts_with("knowledge/topics")
    {
        180.0
    } else {
        90.0
    }
}

/// Exponential freshness decay: weight = 25 * 2^(-age/half_life) - 10, so a
/// page starts at +15 and asymptotically approaches -10 as it ages past its
/// content class's half-life. Unknown age keeps the flat -5 penalty the
/// tiered [`freshness_signal`] uses.
pub(crate) fn freshness_signal_with_half_life(
    age_days: Option<u16>,
    half_life_days: f64,
) -> CredibilitySignal {
    let Some(days) = age_days else {
        return signal(
            "freshness",
            "unknown",
            -5,
            "Missing freshness metadata lowers confidence.",
        );
    };
    let retention = (-std::f64::consts::LN_2 * f64::from(days) / half_life_days).exp();
    let weight = (25.0 * retention - 10.0).round().clamp(-10.0, 15.0) as i16;
    signal(
        "freshness",
        format!("{days} days old (half-life {half_life_days} days)"),
        weight,
        "Freshness confidence decays exponentially at the content class's half-life.",
    )
}

/// Inputs for composing a derived page-level confidence score.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct PageConfidenceInput {
    /// Credibility scores of the sources the page cites.
    pub source_scores: Vec<u8>,
    /// Page age (last modification), in days.
    pub age_days: Option<u16>,
    /// Content-class half-life from [`half_life_days_for_content`].
    pub half_life_days: f64,
    /// Distinct other pages that link to this page.
    pub backlink_count: usize,
}

/// Derived, explainable page-level confidence: cited-source credibility,
/// half-life freshness, and backlink corroboration composed over the same
/// base-50 signal model as [`CredibilityScore`]. Always recomputed from the
/// vault — never persisted as authoritative page state.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PageConfidence {
    pub score: u8,
    pub signals: Vec<CredibilitySignal>,
}

impl PageConfidence {
    pub(crate) fn compose(input: PageConfidenceInput) -> Self {
        let signals = vec![
            cited_sources_signal(&input.source_scores),
            freshness_signal_with_half_life(input.age_days, input.half_life_days),
            backlink_signal(input.backlink_count),
        ];
        let raw_score: i16 = 50 + signals.iter().map(|signal| signal.weight).sum::<i16>();
        Self {
            score: raw_score.clamp(0, 100) as u8,
            signals,
        }
    }
}

/// Average cited-source credibility mapped onto -15..=+15: an average of 50
/// is neutral, 100 is fully supporting, 0 fully undermining. A page citing
/// nothing rests on unverified synthesis and takes a flat -10.
fn cited_sources_signal(source_scores: &[u8]) -> CredibilitySignal {
    if source_scores.is_empty() {
        return signal(
            "cited_sources",
            "none",
            -10,
            "Pages without cited sources rest on unverified synthesis.",
        );
    }
    let total: u32 = source_scores.iter().map(|score| u32::from(*score)).sum();
    let average = total as f64 / source_scores.len() as f64;
    let weight = ((average - 50.0) / 50.0 * 15.0).round() as i16;
    signal(
        "cited_sources",
        format!(
            "{} sources, average credibility {}",
            source_scores.len(),
            average.round()
        ),
        weight,
        "Cited-source credibility carries into the pages built on it.",
    )
}

fn backlink_signal(count: usize) -> CredibilitySignal {
    let (weight, explanation) = match count {
        0 => (-5, "Orphaned pages lack corroborating cross-references."),
        1 => (0, "A single backlink is weak corroboration."),
        2..=4 => (5, "Several backlinks corroborate the page's relevance."),
        _ => (10, "Widely referenced pages are strongly corroborated."),
    };
    signal(
        "backlinks",
        format!("{count} referring pages"),
        weight,
        explanation,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credibility_score_has_explanation() {
        let score = CredibilityScore::evaluate(CredibilityInput {
            source_type: CredibilitySourceType::Official,
            age_days: Some(4),
            author: Some("Gobby Docs Team".to_string()),
            publisher: Some("Gobby".to_string()),
            corroborating_source_ids: vec!["src-a".to_string(), "src-b".to_string()],
        });

        assert!(score.score >= 80);
        for signal_name in [
            "source_type",
            "freshness",
            "author",
            "publisher",
            "corroboration",
        ] {
            let signal = score
                .signals
                .iter()
                .find(|signal| signal.name == signal_name)
                .unwrap_or_else(|| panic!("missing {signal_name} signal"));
            assert!(
                !signal.explanation.trim().is_empty(),
                "{signal_name} signal has explanation"
            );
        }
    }

    #[test]
    fn page_confidence_composes_sources_freshness_and_backlinks() {
        let strong = PageConfidence::compose(PageConfidenceInput {
            source_scores: vec![90, 80],
            age_days: Some(0),
            half_life_days: 180.0,
            backlink_count: 5,
        });
        let weak = PageConfidence::compose(PageConfidenceInput {
            source_scores: Vec::new(),
            age_days: Some(2000),
            half_life_days: 180.0,
            backlink_count: 0,
        });

        // strong: 50 + 11 (avg 85) + 15 (fresh) + 10 (>=5 backlinks) = 86
        assert_eq!(strong.score, 86);
        // weak: 50 - 10 (no sources) - 10 (fully decayed) - 5 (orphan) = 25
        assert_eq!(weak.score, 25);
        for confidence in [&strong, &weak] {
            for name in ["cited_sources", "freshness", "backlinks"] {
                let signal = confidence
                    .signals
                    .iter()
                    .find(|signal| signal.name == name)
                    .unwrap_or_else(|| panic!("missing {name} signal"));
                assert!(
                    !signal.explanation.trim().is_empty(),
                    "{name} signal has explanation"
                );
            }
        }
    }

    #[test]
    fn half_life_parameterizes_freshness_decay() {
        // Same age, longer half-life -> more retained freshness confidence.
        let short = freshness_signal_with_half_life(Some(90), 14.0);
        let long = freshness_signal_with_half_life(Some(90), 180.0);
        assert!(
            long.weight > short.weight,
            "180d half-life ({}) must retain more than 14d ({})",
            long.weight,
            short.weight
        );

        // Curve endpoints: fresh pages start at +15, fully decayed pages
        // bottom out at -10, unknown age keeps the flat -5 penalty.
        assert_eq!(freshness_signal_with_half_life(Some(0), 90.0).weight, 15);
        assert_eq!(
            freshness_signal_with_half_life(Some(u16::MAX), 90.0).weight,
            -10
        );
        assert_eq!(freshness_signal_with_half_life(None, 90.0).weight, -5);

        // One half-life retains half the span: 25 * 0.5 - 10 = 2.5 -> 3.
        assert_eq!(freshness_signal_with_half_life(Some(180), 180.0).weight, 3);
    }

    #[test]
    fn content_class_half_lives_rank_sources_below_synthesis() {
        let sources = half_life_days_for_content(Path::new("knowledge/sources/example.md"));
        let recap = half_life_days_for_content(Path::new("recaps/2026-07-09.md"));
        let concept = half_life_days_for_content(Path::new("knowledge/concepts/gobby.md"));
        let topic = half_life_days_for_content(Path::new("knowledge/topics/dispatch.md"));
        let other = half_life_days_for_content(Path::new("code/files/src/lib.rs.md"));

        assert_eq!(sources, 14.0);
        assert_eq!(recap, 14.0);
        assert_eq!(concept, 180.0);
        assert_eq!(topic, 180.0);
        assert_eq!(other, 90.0);
    }

    #[test]
    fn backlink_factor_tiers_confidence() {
        let scores: Vec<u8> = [0_usize, 1, 2, 5]
            .into_iter()
            .map(|backlinks| {
                PageConfidence::compose(PageConfidenceInput {
                    source_scores: vec![70],
                    age_days: Some(10),
                    half_life_days: 180.0,
                    backlink_count: backlinks,
                })
                .score
            })
            .collect();

        assert!(
            scores.windows(2).all(|pair| pair[0] < pair[1]),
            "confidence must rise monotonically across backlink tiers: {scores:?}"
        );
    }
}
