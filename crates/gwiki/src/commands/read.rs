use std::cmp::Ordering;
use std::path::{Component, Path, PathBuf};

use serde::Serialize;

use crate::markdown::parse_atx_heading;
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ReadTarget, ScopeIdentity, ScopeSelection, WikiError};

const MAX_TITLE_CANDIDATES: usize = 50;
const MAX_TITLE_SEARCH_DEPTH: usize = 64;
const MAX_TITLE_SCAN_ENTRIES: usize = 10_000;

pub(crate) fn execute(
    target: ReadTarget,
    selection: ScopeSelection,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let output = match target {
        ReadTarget::Path(path) => read_path(scope.root(), output_scope, path)?,
        ReadTarget::Title(title) => read_title(scope.root(), output_scope, title)?,
    };
    render(output)
}

fn read_path(
    root: &Path,
    scope: ScopeIdentity,
    requested_path: PathBuf,
) -> Result<ReadOutput, WikiError> {
    let requested = ReadRequested::path(requested_path.display().to_string());
    let wiki_path = match normalize_requested_path(&requested_path) {
        Ok(path) => path,
        Err(degradation) => return Ok(ReadOutput::invalid_request(scope, requested, degradation)),
    };

    if let Some(degradation) = readable_path_degradation(&wiki_path) {
        return Ok(ReadOutput::invalid_request(scope, requested, degradation));
    }

    let absolute_path = root.join(&wiki_path);
    if !absolute_path.is_file() {
        return Ok(ReadOutput::not_found(
            scope,
            requested,
            Some(wiki_path),
            Some(absolute_path),
            ReadDegradation::not_found("No scoped wiki document exists at the requested path."),
        ));
    }

    read_existing_path(root, scope, requested, wiki_path)
}

fn read_title(root: &Path, scope: ScopeIdentity, title: String) -> Result<ReadOutput, WikiError> {
    let requested = ReadRequested::title(title.clone());
    if title.trim().is_empty() {
        return Ok(ReadOutput::invalid_request(
            scope,
            requested,
            ReadDegradation::invalid_request("Title must not be empty."),
        ));
    }

    let mut candidates = title_candidates(root, &title, MAX_TITLE_CANDIDATES)?;
    candidates.sort_by(|left, right| left.wiki_path.cmp(&right.wiki_path));
    match candidates.len().cmp(&1) {
        Ordering::Less => Ok(ReadOutput::not_found(
            scope,
            requested,
            None,
            None,
            ReadDegradation::not_found("No scoped wiki document has the requested title."),
        )),
        Ordering::Equal => {
            let candidate = candidates.remove(0);
            read_existing_path(root, scope, requested, candidate.wiki_path)
        }
        Ordering::Greater => Ok(ReadOutput::ambiguous(scope, requested, candidates)),
    }
}

fn read_existing_path(
    root: &Path,
    scope: ScopeIdentity,
    requested: ReadRequested,
    wiki_path: PathBuf,
) -> Result<ReadOutput, WikiError> {
    let absolute_path = root.join(&wiki_path);
    let content = std::fs::read_to_string(&absolute_path).map_err(|error| WikiError::Io {
        action: "read wiki document",
        path: Some(absolute_path.clone()),
        source: error,
    })?;
    Ok(ReadOutput::found(
        scope,
        requested,
        wiki_path,
        absolute_path,
        first_heading(&content),
        content,
    ))
}

fn normalize_requested_path(path: &Path) -> Result<PathBuf, ReadDegradation> {
    if path.is_absolute() {
        return Err(ReadDegradation::invalid_request(
            "Read paths must be vault-relative, not absolute.",
        ));
    }

    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(value) => normalized.push(value),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(ReadDegradation::invalid_request(
                    "Read paths must stay inside the selected wiki scope.",
                ));
            }
        }
    }

    if normalized.as_os_str().is_empty() {
        return Err(ReadDegradation::invalid_request(
            "Read path must identify a wiki document.",
        ));
    }

    Ok(normalized)
}

fn readable_path_degradation(path: &Path) -> Option<ReadDegradation> {
    if path.extension().and_then(|extension| extension.to_str()) != Some("md") {
        return Some(ReadDegradation::invalid_request(
            "Read only serves Markdown wiki documents.",
        ));
    }

    if is_readable_wiki_path(path) {
        None
    } else {
        Some(ReadDegradation::invalid_request(
            "Read paths must target canonical wiki documents under raw/INDEX.md, _index.md, log.md, or wiki/.",
        ))
    }
}

fn is_readable_wiki_path(path: &Path) -> bool {
    if matches!(path.to_str(), Some("raw/INDEX.md" | "_index.md" | "log.md")) {
        return true;
    }

    matches!(
        normal_components(path).as_slice(),
        ["wiki", "sources", ..]
            | ["wiki", "concepts", ..]
            | ["wiki", "topics", ..]
            | ["wiki", "code", ..]
    )
}

fn title_candidates(
    root: &Path,
    title: &str,
    max_results: usize,
) -> Result<Vec<ReadCandidate>, WikiError> {
    title_candidates_with_scan_budget(root, title, max_results, MAX_TITLE_SCAN_ENTRIES)
}

fn title_candidates_with_scan_budget(
    root: &Path,
    title: &str,
    max_results: usize,
    max_scanned_entries: usize,
) -> Result<Vec<ReadCandidate>, WikiError> {
    let mut candidates = Vec::new();
    let mut search = TitleCandidateSearch {
        max_results,
        max_scanned_entries,
        scanned_entries: 0,
    };
    collect_title_candidates(root, root, title, 0, &mut search, &mut candidates)?;
    Ok(candidates)
}

struct TitleCandidateSearch {
    max_results: usize,
    max_scanned_entries: usize,
    scanned_entries: usize,
}

fn collect_title_candidates(
    root: &Path,
    dir: &Path,
    title: &str,
    depth: usize,
    search: &mut TitleCandidateSearch,
    candidates: &mut Vec<ReadCandidate>,
) -> Result<(), WikiError> {
    if candidates.len() >= search.max_results
        || depth > MAX_TITLE_SEARCH_DEPTH
        || search.scanned_entries >= search.max_scanned_entries
    {
        return Ok(());
    }

    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read wiki directory",
                path: Some(dir.to_path_buf()),
                source: error,
            });
        }
    };

    for entry in entries {
        if candidates.len() >= search.max_results
            || search.scanned_entries >= search.max_scanned_entries
        {
            return Ok(());
        }
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read wiki directory entry",
            path: Some(dir.to_path_buf()),
            source: error,
        })?;
        search.scanned_entries += 1;
        let path = entry.path();
        if path.is_dir() {
            collect_title_candidates(root, &path, title, depth + 1, search, candidates)?;
            continue;
        }

        let Some(relative) = path.strip_prefix(root).ok() else {
            continue;
        };
        if !is_readable_wiki_path(relative) {
            continue;
        }

        let content = std::fs::read_to_string(&path).map_err(|error| WikiError::Io {
            action: "read wiki document",
            path: Some(path.clone()),
            source: error,
        })?;
        if first_heading(&content).as_deref() == Some(title) {
            candidates.push(ReadCandidate {
                wiki_path: relative.to_path_buf(),
                title: Some(title.to_string()),
            });
            if candidates.len() >= search.max_results {
                return Ok(());
            }
        }
    }

    Ok(())
}

fn first_heading(content: &str) -> Option<String> {
    content.lines().find_map(|line| {
        parse_atx_heading(line)
            .map(|(_, heading)| heading)
            .filter(|heading| !heading.is_empty())
    })
}

fn normal_components(path: &Path) -> Vec<&str> {
    path.components()
        .filter_map(|component| match component {
            Component::Normal(value) => value.to_str(),
            _ => None,
        })
        .collect()
}

fn render(output: ReadOutput) -> Result<CommandOutcome, WikiError> {
    let scope = output.scope.clone();
    let text = render_text(&output);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize read output",
        path: None,
        source: error,
    })?;
    Ok(super::scoped_outcome("read", &scope, payload, text))
}

fn render_text(output: &ReadOutput) -> String {
    if let Some(content) = &output.content {
        return content.clone();
    }

    let mut text = format!(
        "Wiki read {}\nScope: {}\nRequested: {} {}\n",
        output.status, output.scope, output.requested.kind, output.requested.value
    );
    for degradation in &output.degradations {
        text.push_str("- ");
        text.push_str(degradation.display_label());
        text.push_str(": ");
        text.push_str(&degradation.message);
        text.push_str(" Guidance: ");
        text.push_str(degradation.guidance);
        text.push('\n');
    }
    text
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct ReadOutput {
    command: &'static str,
    scope: ScopeIdentity,
    requested: ReadRequested,
    status: &'static str,
    wiki_path: Option<PathBuf>,
    absolute_path: Option<PathBuf>,
    title: Option<String>,
    content_format: &'static str,
    content: Option<String>,
    byte_len: Option<usize>,
    truncated: bool,
    candidates: Vec<ReadCandidate>,
    degradations: Vec<ReadDegradation>,
}

impl ReadOutput {
    fn found(
        scope: ScopeIdentity,
        requested: ReadRequested,
        wiki_path: PathBuf,
        absolute_path: PathBuf,
        title: Option<String>,
        content: String,
    ) -> Self {
        let byte_len = content.len();
        Self {
            command: "read",
            scope,
            requested,
            status: "found",
            wiki_path: Some(wiki_path),
            absolute_path: Some(absolute_path),
            title,
            content_format: "markdown",
            content: Some(content),
            byte_len: Some(byte_len),
            truncated: false,
            candidates: Vec::new(),
            degradations: Vec::new(),
        }
    }

    fn not_found(
        scope: ScopeIdentity,
        requested: ReadRequested,
        wiki_path: Option<PathBuf>,
        absolute_path: Option<PathBuf>,
        degradation: ReadDegradation,
    ) -> Self {
        Self::empty(
            scope,
            requested,
            "not_found",
            wiki_path,
            absolute_path,
            vec![degradation],
        )
    }

    fn invalid_request(
        scope: ScopeIdentity,
        requested: ReadRequested,
        degradation: ReadDegradation,
    ) -> Self {
        Self::empty(
            scope,
            requested,
            "invalid_request",
            None,
            None,
            vec![degradation],
        )
    }

    fn ambiguous(
        scope: ScopeIdentity,
        requested: ReadRequested,
        candidates: Vec<ReadCandidate>,
    ) -> Self {
        let mut output = Self::empty(
            scope,
            requested,
            "ambiguous",
            None,
            None,
            vec![ReadDegradation::ambiguous(
                "Multiple scoped wiki documents have the requested title.",
            )],
        );
        output.candidates = candidates;
        output
    }

    fn empty(
        scope: ScopeIdentity,
        requested: ReadRequested,
        status: &'static str,
        wiki_path: Option<PathBuf>,
        absolute_path: Option<PathBuf>,
        degradations: Vec<ReadDegradation>,
    ) -> Self {
        Self {
            command: "read",
            scope,
            requested,
            status,
            wiki_path,
            absolute_path,
            title: None,
            content_format: "markdown",
            content: None,
            byte_len: None,
            truncated: false,
            candidates: Vec::new(),
            degradations,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct ReadRequested {
    kind: &'static str,
    value: String,
}

impl ReadRequested {
    fn path(value: String) -> Self {
        Self {
            kind: "path",
            value,
        }
    }

    fn title(value: String) -> Self {
        Self {
            kind: "title",
            value,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct ReadCandidate {
    wiki_path: PathBuf,
    title: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct ReadDegradation {
    reason: &'static str,
    message: String,
    guidance: &'static str,
}

impl ReadDegradation {
    fn display_label(&self) -> &'static str {
        match self.reason {
            "invalid_request" => "Invalid request",
            "not_found" => "Not found",
            "ambiguous" => "Ambiguous title",
            _ => "Degraded",
        }
    }

    fn invalid_request(message: impl Into<String>) -> Self {
        Self {
            reason: "invalid_request",
            message: message.into(),
            guidance: "Pass --path with a vault-relative Markdown path or --title with an exact first heading.",
        }
    }

    fn not_found(message: impl Into<String>) -> Self {
        Self {
            reason: "not_found",
            message: message.into(),
            guidance: "Run gwiki search in the same scope or pass an exact vault-relative path.",
        }
    }

    fn ambiguous(message: impl Into<String>) -> Self {
        Self {
            reason: "ambiguous",
            message: message.into(),
            guidance: "Pass --path with one of the returned candidate wiki_path values.",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn title_search_stops_at_max_depth() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut deep = temp.path().to_path_buf();
        for depth in 0..=MAX_TITLE_SEARCH_DEPTH + 2 {
            deep = deep.join(format!("d{depth}"));
        }
        std::fs::create_dir_all(&deep).expect("deep dirs");
        std::fs::write(deep.join("target.md"), "# Deep Target\n").expect("deep markdown");

        let candidates = title_candidates(temp.path(), "Deep Target", MAX_TITLE_CANDIDATES)
            .expect("title search");

        assert!(candidates.is_empty());
    }

    #[test]
    fn title_search_stops_at_scan_budget() {
        let temp = tempfile::tempdir().expect("tempdir");
        let topic_dir = temp.path().join("wiki/topics");
        std::fs::create_dir_all(&topic_dir).expect("topic dir");
        std::fs::write(topic_dir.join("target.md"), "# Target\n").expect("target markdown");

        let candidates =
            title_candidates_with_scan_budget(temp.path(), "Target", MAX_TITLE_CANDIDATES, 0)
                .expect("title search");

        assert!(candidates.is_empty());
    }
}
