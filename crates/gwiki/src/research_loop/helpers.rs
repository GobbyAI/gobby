use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};

use crate::research::ResearchStopReason;

use super::types::{ModelRequest, ResearchAction};

pub(crate) fn parse_model_action(response: &str) -> Result<ResearchAction, String> {
    let candidate = json_candidate(response)?;
    // Local reasoning models routinely append prose after the action object;
    // stream deserialization stops at the first complete top-level value, so
    // trailing text (including text containing braces) is ignored.
    let mut stream = serde_json::Deserializer::from_str(candidate).into_iter::<ResearchAction>();
    match stream.next() {
        Some(Ok(action)) => Ok(action),
        Some(Err(error)) => Err(format!("failed to parse action JSON: {error}")),
        None => Err("model response did not include a JSON object".to_string()),
    }
}

pub(crate) fn render_model_prompt(request: &ModelRequest<'_>) -> String {
    let mut prompt = format!(
        "Research question: {}\nStep: {}/{}\nTokens used: {}/{}\nSources added: {}/{}\n",
        request.question,
        request.step_index,
        request.max_steps,
        request.tokens_used,
        request.max_tokens,
        request.sources_added.len(),
        request.max_sources
    );
    if !request.source_constraints.is_empty() {
        prompt.push_str("Source constraints:\n");
        for constraint in request.source_constraints {
            prompt.push_str("- ");
            prompt.push_str(constraint);
            prompt.push('\n');
        }
    }
    if !request.known_sources.is_empty() {
        prompt.push_str("Observed sources:\n");
        for source in request.known_sources.iter().take(20) {
            prompt.push_str("- ");
            prompt.push_str(source);
            prompt.push('\n');
        }
    }
    if !request.observations.is_empty() {
        prompt.push_str("Recent observations:\n");
        let start = request.observations.len().saturating_sub(8);
        for observation in &request.observations[start..] {
            prompt.push_str("- ");
            prompt.push_str(&observation.action);
            prompt.push_str(": ");
            prompt.push_str(observation.summary.trim());
            prompt.push('\n');
        }
    }
    if !request.gaps.is_empty() {
        prompt.push_str("Recorded gaps:\n");
        for gap in request.gaps.iter().rev().take(8) {
            prompt.push_str("- ");
            prompt.push_str(&gap.reason);
            prompt.push('\n');
        }
    }
    prompt.push_str(
        "Return one JSON object only. Supported actions are: \
         {\"action\":\"ask\",\"query\":\"...\"}, \
         {\"action\":\"search\",\"query\":\"...\"}, \
         {\"action\":\"read\",\"path\":\"...\"}, \
         {\"action\":\"ingest_url\",\"url\":\"https://...\"}, \
         {\"action\":\"ingest_file\",\"path\":\"...\"}, \
         {\"action\":\"accept_note\",\"title\":\"...\",\"body\":\"...\",\"sources\":[\"...\"]}, \
         {\"action\":\"record_gap\",\"reason\":\"...\",\"evidence\":[\"...\"]}, \
         {\"action\":\"finish\",\"reason\":\"...\"}. \
         Accepted notes must cite only observed sources.",
    );
    prompt
}

pub(crate) fn model_system_prompt() -> &'static str {
    "You are gwiki research planning. Choose exactly one source-grounded action as JSON. \
     Do not write accepted notes until cited sources have been observed."
}

/// Slice from the first `{` (after stripping a leading code fence) so the
/// stream deserializer reads the first complete top-level object; everything
/// after that object — closing fences, prose, even stray braces — is ignored.
fn json_candidate(response: &str) -> Result<&str, String> {
    let trimmed = response.trim();
    let trimmed = trimmed
        .strip_prefix("```json")
        .or_else(|| trimmed.strip_prefix("```"))
        .unwrap_or(trimmed)
        .trim();
    let trimmed = trimmed.strip_suffix("```").unwrap_or(trimmed).trim();
    let start = trimmed
        .find('{')
        .ok_or_else(|| "model response did not include a JSON object".to_string())?;
    Ok(&trimmed[start..])
}

pub(crate) fn action_fingerprint(action: &ResearchAction) -> String {
    serde_json::to_string(action).unwrap_or_else(|_| format!("{action:?}"))
}

pub(crate) fn normalize_sources(sources: &[String]) -> Vec<String> {
    let mut normalized = Vec::new();
    let mut seen = HashSet::new();
    for source in sources {
        let source = source.trim();
        if source.is_empty() || !seen.insert(source.to_string()) {
            continue;
        }
        normalized.push(source.to_string());
    }
    normalized
}

pub(crate) fn validate_source_reference(root: &Path, source: &str) -> Result<(), String> {
    if let Ok(url) = url::Url::parse(source) {
        return match url.scheme() {
            "http" | "https" => Ok(()),
            "file" => {
                let path = url
                    .to_file_path()
                    .map_err(|_| "file URL source path is invalid".to_string())?;
                validate_source_path(root, &path)
            }
            scheme => Err(format!("unsupported URL scheme '{scheme}'")),
        };
    }

    let path = Path::new(source);
    validate_source_path(root, path)
}

fn validate_source_path(root: &Path, path: &Path) -> Result<(), String> {
    if path.as_os_str().is_empty() {
        return Err("source path is empty".to_string());
    }
    if path
        .components()
        .any(|component| component == Component::ParentDir)
    {
        return Err("source path cannot contain parent traversal".to_string());
    }
    let resolved_root = root
        .canonicalize()
        .map_err(|_| "wiki scope root does not exist or cannot be resolved".to_string())?;
    if path.is_absolute() {
        let resolved_path = path
            .canonicalize()
            .map_err(|_| "source path does not exist or cannot be resolved".to_string())?;
        if resolved_path.starts_with(&resolved_root) {
            return Ok(());
        }
        return Err("absolute source path is outside the wiki scope".to_string());
    }
    if path
        .components()
        .any(|component| matches!(component, Component::Prefix(_) | Component::RootDir))
    {
        return Err("relative source path must stay inside the wiki scope".to_string());
    }
    let resolved_path = root
        .join(path)
        .canonicalize()
        .map_err(|_| "source path does not exist or cannot be resolved".to_string())?;
    if !resolved_path.starts_with(&resolved_root) {
        return Err("relative source path must stay inside the wiki scope".to_string());
    }
    Ok(())
}

pub(crate) fn source_evidence(root: &Path, source: &str) -> Vec<PathBuf> {
    let path = Path::new(source);
    if path.as_os_str().is_empty() || url::Url::parse(source).is_ok() {
        return Vec::new();
    }
    if path.is_absolute() {
        vec![path.to_path_buf()]
    } else {
        vec![root.join(path)]
    }
}

pub(crate) fn source_path_aliases(root: &Path, source: &str) -> Vec<String> {
    if url::Url::parse(source).is_ok() {
        return Vec::new();
    }
    let path = Path::new(source);
    let mut aliases = Vec::new();
    if path.is_absolute() {
        if let Ok(relative) = path.strip_prefix(root) {
            aliases.push(relative.display().to_string());
        }
    } else {
        aliases.push(root.join(path).display().to_string());
    }
    aliases
}

pub(crate) fn sorted_sources(sources: &HashSet<String>) -> Vec<String> {
    let mut sources = sources.iter().cloned().collect::<Vec<_>>();
    sources.sort();
    sources
}

pub(crate) fn default_stop_message(stop_reason: ResearchStopReason) -> &'static str {
    match stop_reason {
        ResearchStopReason::Finish => "research session completed",
        ResearchStopReason::BudgetExhausted => "research budget exhausted",
        ResearchStopReason::NoProgress => "research stopped after no progress",
        ResearchStopReason::DuplicateFrontier => "research stopped on duplicate frontier",
        ResearchStopReason::SourceBlocked => "research stopped on unsupported source",
        ResearchStopReason::WriteConflict => "research stopped on write conflict",
        ResearchStopReason::AiUnavailable => "research stopped because AI is unavailable",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn action_fingerprint_uses_stable_json() {
        let action = ResearchAction::Search {
            query: "rust parser".to_string(),
        };

        assert_eq!(
            action_fingerprint(&action),
            r#"{"action":"search","query":"rust parser"}"#
        );
    }
}
