use std::collections::HashSet;

use super::*;

pub(crate) fn observation_from_outcome(
    action: &str,
    outcome: &CommandOutcome,
) -> ResearchObservation {
    ResearchObservation::new(action, outcome.result.text.clone())
        .with_sources(outcome_sources(&outcome.result.payload))
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
        if seen.insert(value.clone()) {
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

/// Rough budget heuristic used only when provider usage metadata is unavailable.
/// Estimate conservatively as ceil(words * 1.3) without floating-point drift.
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
        .collect::<std::collections::BTreeSet<_>>();
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
