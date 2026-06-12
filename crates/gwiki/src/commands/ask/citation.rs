use std::collections::HashSet;

use crate::output::{AskCitationCheckOutput, AskOutput};

/// Grounding statuses for [`citation_check`]: every extracted claim overlaps
/// the retrieved evidence, or at least one claim does not.
pub(super) const CITATION_CHECK_SUPPORTED: &str = "supported";
const CITATION_CHECK_UNSUPPORTED: &str = "unsupported_claims";

/// Minimum fraction of a claim's significant tokens that must appear in the
/// retrieved evidence for the claim to count as grounded.
const CLAIM_SUPPORT_THRESHOLD: f64 = 0.5;

/// Claims with fewer significant tokens than this are skipped — short hedges
/// like "The evidence is insufficient." carry no checkable factual content.
const MIN_CLAIM_TOKENS: usize = 3;

/// Check the synthesized answer's claims against the retrieved evidence.
///
/// The synthesis prompt only *asks* the model to stay grounded; this is the
/// post-generation verification. Each sentence-level claim is tokenized and
/// must overlap the evidence corpus (hit titles/snippets/paths, the prompt's
/// evidence excerpts, code citations) above [`CLAIM_SUPPORT_THRESHOLD`],
/// mirroring the spirit of `audit`'s provenance check for persisted prose.
pub(super) fn citation_check(
    answer: &str,
    output: &AskOutput,
    evidence_excerpts: &[String],
) -> AskCitationCheckOutput {
    let evidence = evidence_tokens(output, evidence_excerpts);
    let claims = answer_claims(answer);
    let checked_claims = claims.len();
    let unsupported_claims: Vec<String> = claims
        .into_iter()
        .filter(|claim| !claim_is_supported(claim, &evidence))
        .collect();
    AskCitationCheckOutput {
        status: if unsupported_claims.is_empty() {
            CITATION_CHECK_SUPPORTED
        } else {
            CITATION_CHECK_UNSUPPORTED
        },
        checked_claims,
        unsupported_claims,
    }
}

/// Split the answer into sentence-level claims with enough significant tokens
/// to be checkable. Markdown headings and list markers are stripped first.
fn answer_claims(answer: &str) -> Vec<String> {
    answer
        .lines()
        .map(|line| line.trim().trim_start_matches(['#', '-', '*', '>']).trim())
        .filter(|line| !line.is_empty())
        .flat_map(|line| line.split_inclusive(['.', '!', '?']))
        .map(|sentence| {
            sentence
                .trim()
                .trim_end_matches(['.', '!', '?'])
                .to_string()
        })
        .filter(|sentence| significant_tokens(sentence).len() >= MIN_CLAIM_TOKENS)
        .collect()
}

fn claim_is_supported(claim: &str, evidence: &HashSet<String>) -> bool {
    let tokens = significant_tokens(claim);
    if tokens.is_empty() {
        return true;
    }
    let supported = tokens
        .iter()
        .filter(|token| evidence.contains(token.as_str()))
        .count();
    (supported as f64 / tokens.len() as f64) >= CLAIM_SUPPORT_THRESHOLD
}

fn evidence_tokens(output: &AskOutput, evidence_excerpts: &[String]) -> HashSet<String> {
    let mut evidence = HashSet::new();
    for hit in &output.hits {
        if let Some(title) = &hit.title {
            collect_tokens(title, &mut evidence);
        }
        collect_tokens(&hit.snippet, &mut evidence);
        collect_tokens(&hit.wiki_page.display().to_string(), &mut evidence);
        collect_tokens(&hit.source_path.display().to_string(), &mut evidence);
    }
    for excerpt in evidence_excerpts {
        collect_tokens(excerpt, &mut evidence);
    }
    for citation in &output.code_citations {
        collect_tokens(&citation.file, &mut evidence);
        if let Some(symbol) = &citation.symbol {
            collect_tokens(symbol, &mut evidence);
        }
    }
    evidence
}

fn significant_tokens(text: &str) -> Vec<String> {
    let mut tokens: Vec<String> = Vec::new();
    collect_tokens_into(text, |token| tokens.push(token));
    tokens
}

fn collect_tokens(text: &str, evidence: &mut HashSet<String>) {
    collect_tokens_into(text, |token| {
        evidence.insert(token);
    });
}

/// Tokenize into lowercase alphanumeric runs, keeping only words long enough
/// to carry meaning and dropping common function words.
fn collect_tokens_into(text: &str, mut push: impl FnMut(String)) {
    const STOPWORDS: &[&str] = &[
        "about", "after", "also", "based", "because", "been", "before", "being", "between", "both",
        "does", "each", "either", "from", "have", "into", "its", "more", "most", "only", "other",
        "over", "same", "should", "since", "some", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "through", "under", "uses", "using", "very", "when",
        "where", "which", "while", "will", "with", "within", "would",
    ];
    for token in text
        .split(|c: char| !c.is_alphanumeric())
        .filter(|token| token.len() >= 4)
    {
        let token = token.to_lowercase();
        if !STOPWORDS.contains(&token.as_str()) {
            push(token);
        }
    }
}
