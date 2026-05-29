//! Explainable credibility scoring for raw wiki sources.

use serde::{Deserialize, Serialize};

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
    pub fn score(input: CredibilityInput) -> Self {
        let mut raw_score = 50;
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credibility_score_has_explanation() {
        let score = CredibilityScore::score(CredibilityInput {
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
}
