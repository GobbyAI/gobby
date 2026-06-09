use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs;
use std::io::ErrorKind;
use std::path::{Component, Path, PathBuf};

use serde_json::Value;

use super::notes::{frontmatter_block, materializing_marker_is_stale, yaml_field_eq};
use super::{AuditFinding, AuditSeverity, RESEARCH_NOTE_NAMESPACE, ResearchOptions};
use crate::research_loop::ResearchObservation;
use crate::scope::{self, ScopeKind};
use crate::session::{ResearchCodeCitation, ResearchScope, ResearchSession, research_prompt};
use crate::{CommandOutcome, ScopeSelection, WikiError};

pub(crate) fn observation_from_outcome(
    action: &str,
    outcome: &CommandOutcome,
) -> ResearchObservation {
    ResearchObservation::new(action, outcome.result.text.clone())
        .with_sources(outcome_sources(&outcome.result.payload))
        .with_code_citations(outcome_code_citations(&outcome.result.payload, action))
        .with_degradations(outcome_degradations(&outcome.result.payload))
        .with_changed_paths(outcome_changed_paths(&outcome.result.payload))
}

pub(crate) fn outcome_sources(payload: &Value) -> Vec<String> {
    const SOURCE_KEYS: &[&str] = &[
        "absolute_path",
        "asset_path",
        "final_url",
        "location",
        "raw_path",
        "requested_url",
        "source_path",
        "sources",
        "wiki_path",
    ];
    let mut sources = Vec::new();
    collect_keyed_strings(payload, SOURCE_KEYS, &mut sources);
    dedup_strings(sources)
}

pub(crate) fn outcome_changed_paths(payload: &Value) -> Vec<PathBuf> {
    const PATH_KEYS: &[&str] = &["absolute_path", "asset_path", "raw_path", "wiki_path"];
    let mut paths = Vec::new();
    collect_keyed_strings(payload, PATH_KEYS, &mut paths);
    dedup_strings(paths)
        .into_iter()
        .map(PathBuf::from)
        .collect()
}

pub(crate) fn outcome_code_citations(
    payload: &Value,
    provenance: &str,
) -> Vec<ResearchCodeCitation> {
    let mut citations = Vec::new();
    if let Some(values) = payload.get("code_citations").and_then(Value::as_array) {
        for value in values {
            let Some(file) = value.get("file").and_then(Value::as_str) else {
                continue;
            };
            let Some(file) = sanitize_code_path(file) else {
                continue;
            };
            citations.push(ResearchCodeCitation {
                file,
                line: value
                    .get("line")
                    .and_then(Value::as_u64)
                    .and_then(|line| usize::try_from(line).ok()),
                symbol: value
                    .get("symbol")
                    .and_then(Value::as_str)
                    .map(|symbol| symbol.trim().to_string())
                    .filter(|symbol| !symbol.is_empty()),
                provenance: vec![provenance.to_string()],
            });
        }
    }
    dedup_code_citations(citations)
}

pub(crate) fn outcome_degradations(payload: &Value) -> Vec<String> {
    let mut degradations = Vec::new();
    collect_keyed_strings(
        payload,
        &["degradation", "degradations", "degraded_sources"],
        &mut degradations,
    );
    dedup_strings(degradations)
}

pub(crate) fn dedup_code_citations(
    citations: Vec<ResearchCodeCitation>,
) -> Vec<ResearchCodeCitation> {
    let mut indexes = HashMap::new();
    let mut seen = HashSet::new();
    let mut deduped = Vec::new();
    for mut citation in citations {
        dedup_code_citation_provenance(&mut citation.provenance);
        let key = (
            citation.file.clone(),
            citation.line,
            citation.symbol.clone(),
        );
        if seen.insert(key.clone()) {
            indexes.insert(key, deduped.len());
            deduped.push(citation);
        } else if let Some(index) = indexes.get(&key).copied() {
            let existing: &mut ResearchCodeCitation = &mut deduped[index];
            for provenance in citation.provenance.drain(..) {
                if !existing.provenance.contains(&provenance) {
                    existing.provenance.push(provenance);
                }
            }
        }
    }
    deduped
}

fn sanitize_code_path(path: &str) -> Option<String> {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        return None;
    }
    let path = Path::new(trimmed);
    if path.is_absolute() {
        return None;
    }
    let mut has_normal_component = false;
    for component in path.components() {
        match component {
            Component::Normal(_) => has_normal_component = true,
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => return None,
        }
    }
    has_normal_component.then(|| trimmed.to_string())
}

fn dedup_code_citation_provenance(provenance: &mut Vec<String>) {
    let mut seen = HashSet::new();
    provenance.retain(|source| seen.insert(source.clone()));
}

pub(crate) fn collect_keyed_strings(value: &Value, keys: &[&str], out: &mut Vec<String>) {
    match value {
        Value::Object(map) => {
            for (key, value) in map {
                if keys.iter().any(|candidate| candidate == key) {
                    collect_strings(value, out);
                }
                collect_keyed_strings(value, keys, out);
            }
        }
        Value::Array(values) => {
            for value in values {
                collect_keyed_strings(value, keys, out);
            }
        }
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {}
    }
}

pub(crate) fn collect_strings(value: &Value, out: &mut Vec<String>) {
    match value {
        Value::String(value) if !value.trim().is_empty() => out.push(value.trim().to_string()),
        Value::Array(values) => {
            for value in values {
                collect_strings(value, out);
            }
        }
        Value::Object(map) => {
            for value in map.values() {
                collect_strings(value, out);
            }
        }
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {}
    }
}

pub(crate) fn dedup_strings(values: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut deduped = Vec::new();
    for value in values {
        if !seen.contains(&value) {
            seen.insert(value.clone());
            deduped.push(value);
        }
    }
    deduped
}

pub(crate) fn scope_selection_from_research_scope(scope: &ResearchScope) -> ScopeSelection {
    match scope {
        ResearchScope::Project { root, .. } => ScopeSelection::project(root.clone()),
        ResearchScope::Topic { name, .. } => ScopeSelection::topic(name.clone()),
    }
}

/// Soft budget heuristic used only when provider usage metadata is unavailable.
/// Expect a margin of error; this keeps control-loop budgets conservative
/// without claiming tokenizer-level precision.
pub(crate) fn estimate_tokens(text: &str) -> usize {
    let words = text.split_whitespace().count();
    if words == 0 {
        0
    } else {
        words.saturating_mul(13).saturating_add(9) / 10
    }
}

pub(crate) fn load_or_create_session(
    options: &ResearchOptions,
) -> Result<ResearchSession, WikiError> {
    let checkpoint_path = ResearchSession::checkpoint_path(options.scope.root());
    let mut session = if checkpoint_path.exists() {
        ResearchSession::load_checkpoint(options.scope.root())?
    } else {
        ResearchSession::new(
            options.question.clone(),
            options.scope.clone(),
            options.source_constraints.clone(),
            1,
            None,
        )?
    };
    session.question = options.question.clone();
    session.source_constraints = options.source_constraints.clone();
    session.agent_count = 1;
    session.dispatch_task_id = None;
    session.dispatch = None;
    session.prompt = research_prompt(&session.question, &session.source_constraints, 1);
    Ok(session)
}

pub(crate) fn deterministic_audit_findings(
    vault_root: &Path,
    session: &ResearchSession,
) -> Result<Vec<AuditFinding>, WikiError> {
    let research_dir = vault_root.join("raw").join("research");
    let entries = match fs::read_dir(&research_dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read raw research directory",
                path: Some(research_dir),
                source: error,
            });
        }
    };

    let mut note_paths = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read raw research directory entry",
            path: Some(research_dir.clone()),
            source: error,
        })?;
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) == Some("md") {
            note_paths.push(path);
        }
    }
    note_paths.sort();

    let accepted_paths = session
        .accepted_notes
        .iter()
        .map(|note| note.path.clone())
        .collect::<BTreeSet<_>>();
    let mut findings = Vec::new();
    for path in note_paths {
        let contents = fs::read_to_string(&path).map_err(|error| WikiError::Io {
            action: "read raw research note for audit",
            path: Some(path.clone()),
            source: error,
        })?;
        let Some(frontmatter) = frontmatter_block(&contents) else {
            continue;
        };
        if yaml_field_eq(frontmatter, "research_status", "materializing")
            && materializing_marker_is_stale(&path)
        {
            findings.push(AuditFinding {
                fingerprint: audit_fingerprint("stale-materializing", &path),
                severity: AuditSeverity::Warning,
                kind: "orphaned_raw_research_note".to_string(),
                message: "materializing research note marker is stale".to_string(),
                evidence: vec![path.clone()],
                validation_status: "deterministic".to_string(),
            });
            continue;
        }
        if yaml_field_eq(frontmatter, "research_status", "completed")
            && !accepted_paths.contains(&path)
        {
            findings.push(AuditFinding {
                fingerprint: audit_fingerprint("untracked-completed-note", &path),
                severity: AuditSeverity::Info,
                kind: "orphaned_raw_research_note".to_string(),
                message: "completed research note is not listed in the active checkpoint"
                    .to_string(),
                evidence: vec![path],
                validation_status: "deterministic".to_string(),
            });
        }
    }

    Ok(findings)
}

pub(crate) fn audit_fingerprint(kind: &str, path: &Path) -> String {
    let input = format!("{kind}:{}", path.display());
    uuid::Uuid::new_v5(&RESEARCH_NOTE_NAMESPACE, input.as_bytes()).to_string()
}

pub fn resolve_scope(selection: &ScopeSelection) -> Result<ResearchScope, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        action: "read current directory",
        path: None,
        source: error,
    })?;
    let scope = scope::resolve(selection, &cwd)?;
    Ok(research_scope_from_resolved(&scope))
}

pub fn research_scope_from_resolved(scope: &scope::ResolvedScope) -> ResearchScope {
    match scope.kind() {
        ScopeKind::Topic { name } => ResearchScope::topic(name.clone(), scope.root().to_path_buf()),
        ScopeKind::Project { project_id, .. } => {
            ResearchScope::project_for_id(project_id.clone(), scope.root().to_path_buf())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dedup_strings_preserves_first_seen_order() {
        assert_eq!(
            dedup_strings(vec![
                "a".to_string(),
                "b".to_string(),
                "a".to_string(),
                "c".to_string(),
            ]),
            vec!["a", "b", "c"]
        );
    }

    #[test]
    fn dedup_code_citations_preserves_first_seen_order() {
        let citation = ResearchCodeCitation {
            file: "src/lib.rs".to_string(),
            line: Some(7),
            symbol: Some("handler".to_string()),
            provenance: vec!["search".to_string()],
        };
        let other = ResearchCodeCitation {
            symbol: Some("service".to_string()),
            ..citation.clone()
        };
        let reranked = ResearchCodeCitation {
            provenance: vec![
                "rerank".to_string(),
                "search".to_string(),
                "rerank".to_string(),
            ],
            ..citation.clone()
        };
        let merged = ResearchCodeCitation {
            provenance: vec!["search".to_string(), "rerank".to_string()],
            ..citation.clone()
        };

        assert_eq!(
            dedup_code_citations(vec![citation, other.clone(), reranked]),
            vec![merged, other]
        );
    }

    #[test]
    fn outcome_code_citations_rejects_unsafe_paths() {
        let payload = serde_json::json!({
            "code_citations": [
                {"file": " src/lib.rs ", "line": 7, "symbol": "handler"},
                {"file": ""},
                {"file": "/tmp/lib.rs"},
                {"file": "../src/lib.rs"},
                {"file": "src/../lib.rs"}
            ]
        });

        let citations = outcome_code_citations(&payload, "search");

        assert_eq!(citations.len(), 1);
        assert_eq!(citations[0].file, "src/lib.rs");
    }

    #[test]
    fn estimate_tokens_uses_one_point_three_word_heuristic() {
        assert_eq!(estimate_tokens(""), 0);
        assert_eq!(estimate_tokens("one"), 2);
        assert_eq!(estimate_tokens("one two three"), 4);
        assert_eq!(estimate_tokens("one two three four"), 6);
        assert_eq!(
            estimate_tokens("one two three four five six seven eight nine ten"),
            13
        );
    }
}
