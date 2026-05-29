use std::fs;
use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::session::{CompileState, ResearchSession};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompileRequest {
    pub topic: String,
    pub outline: Vec<String>,
    pub target_page: Option<PathBuf>,
    pub write_intent: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompileOutcome {
    pub bundle: CompileBundle,
    pub state: CompileState,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompileBundle {
    pub handoff_id: String,
    pub topic: String,
    pub outline: Vec<String>,
    pub accepted_sources: Vec<AcceptedCompileSource>,
    pub citations: Vec<String>,
    pub conflicting_claims: Vec<String>,
    pub missing_evidence: Vec<String>,
    pub target_page: Option<PathBuf>,
    pub write_intent: bool,
    pub path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AcceptedCompileSource {
    pub title: String,
    pub path: PathBuf,
    pub chunks: Vec<String>,
}

pub fn prepare_handoff(
    session: &mut ResearchSession,
    request: CompileRequest,
) -> Result<CompileOutcome, WikiError> {
    if request.topic.trim().is_empty() {
        return Err(WikiError::InvalidInput {
            field: "topic",
            message: "compile handoff requires a topic".to_string(),
        });
    }

    let handoff_id = format!(
        "compile-{}-{}",
        slugify(&request.topic),
        unix_timestamp_ms()
    );
    let bundle_path = session
        .scope
        .root()
        .join(".gwiki")
        .join("compile")
        .join(format!("{handoff_id}.md"));
    let collected_sources = collect_accepted_sources(session)?;
    let bundle = CompileBundle {
        handoff_id: handoff_id.clone(),
        topic: request.topic,
        outline: request.outline,
        accepted_sources: collected_sources.accepted_sources,
        citations: collected_sources.citations,
        conflicting_claims: collected_sources.conflicting_claims,
        missing_evidence: collected_sources.missing_evidence,
        target_page: request.target_page,
        write_intent: request.write_intent,
        path: bundle_path,
    };
    let rendered = render_bundle(&bundle);

    if let Some(parent) = bundle.path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create compile handoff directory",
            path: Some(parent.to_path_buf()),
            source: error.to_string(),
        })?;
    }
    fs::write(&bundle.path, &rendered).map_err(|error| WikiError::Io {
        action: "write compile handoff bundle",
        path: Some(bundle.path.clone()),
        source: error.to_string(),
    })?;

    if bundle.write_intent
        && let Some(target_page) = &bundle.target_page
    {
        write_target_page(target_page, &rendered)?;
    }

    let state = CompileState {
        handoff_id,
        topic: bundle.topic.clone(),
        bundle_path: bundle.path.clone(),
        selected_note_paths: bundle
            .accepted_sources
            .iter()
            .map(|source| source.path.clone())
            .collect(),
        selected_source_titles: bundle
            .accepted_sources
            .iter()
            .map(|source| source.title.clone())
            .collect(),
        citations: bundle.citations.clone(),
        conflicting_claims: bundle.conflicting_claims.clone(),
        missing_evidence: bundle.missing_evidence.clone(),
        write_intent: bundle.write_intent,
    };
    session.record_compile_state(state.clone())?;

    Ok(CompileOutcome { bundle, state })
}

#[derive(Debug, Default)]
struct CollectedSources {
    accepted_sources: Vec<AcceptedCompileSource>,
    citations: Vec<String>,
    conflicting_claims: Vec<String>,
    missing_evidence: Vec<String>,
}

fn collect_accepted_sources(session: &ResearchSession) -> Result<CollectedSources, WikiError> {
    let mut accepted_sources = Vec::new();
    let mut citations = Vec::new();
    let mut conflicting_claims = Vec::new();
    let mut missing_evidence = Vec::new();

    for note in &session.accepted_notes {
        let path = note_path(session.scope.root(), &note.path);
        if !path_is_in_scope(&path, session.scope.root()) {
            continue;
        }
        let text = fs::read_to_string(&path).map_err(|error| WikiError::Io {
            action: "read accepted research note",
            path: Some(path.clone()),
            source: error.to_string(),
        })?;
        let note_sections = parse_note_sections(&text);
        extend_unique(&mut citations, note_sections.citations);
        extend_unique(&mut conflicting_claims, note_sections.conflicting_claims);
        extend_unique(&mut missing_evidence, note_sections.missing_evidence);
        accepted_sources.push(AcceptedCompileSource {
            title: note.title.clone(),
            path,
            chunks: note_sections.chunks,
        });
    }

    Ok(CollectedSources {
        accepted_sources,
        citations,
        conflicting_claims,
        missing_evidence,
    })
}

#[derive(Debug, Default)]
struct ParsedNoteSections {
    citations: Vec<String>,
    conflicting_claims: Vec<String>,
    missing_evidence: Vec<String>,
    chunks: Vec<String>,
}

fn parse_note_sections(text: &str) -> ParsedNoteSections {
    let mut sections = ParsedNoteSections::default();
    for line in body_lines(text) {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if let Some(value) = prefixed_value(trimmed, &["citation:"]) {
            sections.citations.push(value.to_string());
            continue;
        }
        if let Some(value) = prefixed_value(trimmed, &["conflict:", "conflicting claim:"]) {
            sections.conflicting_claims.push(value.to_string());
            continue;
        }
        if let Some(value) = prefixed_value(trimmed, &["gap:", "missing evidence:"]) {
            sections.missing_evidence.push(value.to_string());
            continue;
        }
        sections.chunks.push(trimmed.to_string());
    }
    sections
}

fn body_lines(text: &str) -> Vec<&str> {
    let mut lines = text.lines();
    if lines.next().map(str::trim) != Some("---") {
        return text.lines().collect();
    }

    for line in lines.by_ref() {
        if line.trim() == "---" {
            return lines.collect();
        }
    }
    Vec::new()
}

fn prefixed_value<'a>(line: &'a str, prefixes: &[&str]) -> Option<&'a str> {
    let lower = line.to_ascii_lowercase();
    for prefix in prefixes {
        if lower.starts_with(prefix) {
            let value = line[prefix.len()..].trim();
            if !value.is_empty() {
                return Some(value);
            }
        }
    }
    None
}

fn extend_unique(target: &mut Vec<String>, values: Vec<String>) {
    for value in values {
        if !target.contains(&value) {
            target.push(value);
        }
    }
}

fn note_path(root: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    }
}

fn path_is_in_scope(path: &Path, root: &Path) -> bool {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    if let Ok(path) = path.canonicalize() {
        return path.starts_with(root);
    }
    path.starts_with(root)
}

fn render_bundle(bundle: &CompileBundle) -> String {
    let mut rendered = String::new();
    rendered.push_str("# Compile bundle: ");
    rendered.push_str(&bundle.topic);
    rendered.push_str("\n\n");

    render_list_section(&mut rendered, "Topic outline", &bundle.outline);

    rendered.push_str("## Accepted sources\n\n");
    if bundle.accepted_sources.is_empty() {
        rendered.push_str("- None recorded.\n");
    } else {
        for source in &bundle.accepted_sources {
            rendered.push_str("- ");
            rendered.push_str(&source.title);
            rendered.push_str(" (");
            rendered.push_str(&source.path.to_string_lossy());
            rendered.push_str(")\n");
            for chunk in &source.chunks {
                rendered.push_str("  - ");
                rendered.push_str(chunk);
                rendered.push('\n');
            }
        }
    }
    rendered.push('\n');

    render_list_section(&mut rendered, "Citations", &bundle.citations);
    render_list_section(
        &mut rendered,
        "Conflicting claims",
        &bundle.conflicting_claims,
    );
    render_list_section(&mut rendered, "Missing evidence", &bundle.missing_evidence);

    rendered
}

fn render_list_section(rendered: &mut String, title: &str, values: &[String]) {
    rendered.push_str("## ");
    rendered.push_str(title);
    rendered.push_str("\n\n");
    if values.is_empty() {
        rendered.push_str("- None recorded.\n\n");
        return;
    }
    for value in values {
        rendered.push_str("- ");
        rendered.push_str(value);
        rendered.push('\n');
    }
    rendered.push('\n');
}

fn write_target_page(target_page: &Path, rendered: &str) -> Result<(), WikiError> {
    if let Some(parent) = target_page.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create compile target directory",
            path: Some(parent.to_path_buf()),
            source: error.to_string(),
        })?;
    }
    fs::write(target_page, rendered).map_err(|error| WikiError::Io {
        action: "write compile target page",
        path: Some(target_page.to_path_buf()),
        source: error.to_string(),
    })
}

fn slugify(topic: &str) -> String {
    let mut slug = String::new();
    let mut last_was_dash = false;
    for ch in topic.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            last_was_dash = false;
        } else if !last_was_dash && !slug.is_empty() {
            slug.push('-');
            last_was_dash = true;
        }
    }
    while slug.ends_with('-') {
        slug.pop();
    }
    if slug.is_empty() {
        "handoff".to_string()
    } else {
        slug
    }
}

fn unix_timestamp_ms() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session::{AcceptedResearchNote, ResearchScope, ResearchSession};

    fn session_with_note(
        scope: &ResearchScope,
        title: &str,
        relative_path: &str,
    ) -> ResearchSession {
        ResearchSession {
            session_id: "research-compile-test".to_string(),
            question: "How should compile handoff work?".to_string(),
            prompt: "Compile source-grounded research".to_string(),
            scope: scope.clone(),
            source_constraints: vec!["accepted notes only".to_string()],
            agent_count: 1,
            dispatch_task_id: Some("#302".to_string()),
            dispatch: None,
            accepted_notes: vec![AcceptedResearchNote {
                title: title.to_string(),
                path: scope.root().join(relative_path),
            }],
            compile_state: None,
        }
    }

    #[test]
    fn compile_bundle_contains_required_sections() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let note_path = scope.root().join("raw/research/compile.md");
        std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(
            &note_path,
            "---\ntitle: Compile behavior\nsource: daemon notes\n---\n\nCitation: Example Docs, Compile API\nConflict: Workers disagree about overwrite behavior.\nGap: Missing benchmark evidence.\nAccepted chunk about durable synthesis handoff.",
        )
        .expect("note written");
        let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

        let outcome = prepare_handoff(
            &mut session,
            CompileRequest {
                topic: "Compile behavior".to_string(),
                outline: vec![
                    "Durable handoff".to_string(),
                    "Synthesis inputs".to_string(),
                ],
                target_page: Some(scope.root().join("compile-behavior.md")),
                write_intent: false,
            },
        )
        .expect("compile handoff prepared");

        assert_eq!(outcome.bundle.outline.len(), 2);
        assert_eq!(outcome.bundle.accepted_sources.len(), 1);
        assert_eq!(outcome.bundle.citations, vec!["Example Docs, Compile API"]);
        assert_eq!(
            outcome.bundle.conflicting_claims,
            vec!["Workers disagree about overwrite behavior."]
        );
        assert_eq!(
            outcome.bundle.missing_evidence,
            vec!["Missing benchmark evidence."]
        );

        let rendered = std::fs::read_to_string(&outcome.bundle.path).expect("bundle written");
        assert!(rendered.contains("## Topic outline"));
        assert!(rendered.contains("## Accepted sources"));
        assert!(rendered.contains("## Citations"));
        assert!(rendered.contains("## Conflicting claims"));
        assert!(rendered.contains("## Missing evidence"));
    }

    #[test]
    fn compile_handoff_is_non_destructive_by_default() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let page_path = scope.root().join("compile-behavior.md");
        std::fs::write(&page_path, "human-authored wiki page").expect("page written");
        let note_path = scope.root().join("raw/research/compile.md");
        std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(&note_path, "Citation: Example Docs").expect("note written");
        let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

        let outcome = prepare_handoff(
            &mut session,
            CompileRequest {
                topic: "Compile behavior".to_string(),
                outline: vec!["Durable handoff".to_string()],
                target_page: Some(page_path.clone()),
                write_intent: false,
            },
        )
        .expect("compile handoff prepared");

        assert_eq!(
            std::fs::read_to_string(&page_path).expect("page retained"),
            "human-authored wiki page"
        );
        assert_ne!(outcome.bundle.path, page_path);
        assert!(!outcome.state.write_intent);
    }

    #[test]
    fn compile_bundle_is_scope_filtered() {
        let in_scope = tempfile::tempdir().expect("in scope tempdir");
        let out_of_scope = tempfile::tempdir().expect("out of scope tempdir");
        let scope = ResearchScope::project(in_scope.path());
        let in_scope_path = scope.root().join("raw/research/in-scope.md");
        std::fs::create_dir_all(in_scope_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(&in_scope_path, "Citation: In-scope citation").expect("note written");
        let mut session = session_with_note(&scope, "In scope", "raw/research/in-scope.md");
        session.accepted_notes.push(AcceptedResearchNote {
            title: "Out of scope".to_string(),
            path: out_of_scope.path().join("raw/research/out-of-scope.md"),
        });

        let outcome = prepare_handoff(
            &mut session,
            CompileRequest {
                topic: "Scoped compile".to_string(),
                outline: vec!["Scoped sources".to_string()],
                target_page: None,
                write_intent: false,
            },
        )
        .expect("compile handoff prepared");

        assert_eq!(outcome.bundle.accepted_sources.len(), 1);
        assert_eq!(outcome.bundle.accepted_sources[0].title, "In scope");
        assert!(
            outcome.bundle.accepted_sources[0]
                .path
                .starts_with(scope.root())
        );
        assert!(
            !outcome
                .bundle
                .accepted_sources
                .iter()
                .any(|source| source.path.starts_with(out_of_scope.path()))
        );
    }
}
