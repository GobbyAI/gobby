use std::fmt;
use std::io::Write;
use std::path::PathBuf;

use clap::ValueEnum;

use crate::{CommandResult, ScopeIdentity};

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
pub enum Format {
    Json,
    Text,
}

#[derive(Debug)]
pub enum OutputError {
    Io(std::io::Error),
    Json(serde_json::Error),
}

impl fmt::Display for OutputError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "output write failed: {error}"),
            Self::Json(error) => write!(f, "JSON rendering failed: {error}"),
        }
    }
}

impl std::error::Error for OutputError {}

impl From<std::io::Error> for OutputError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<serde_json::Error> for OutputError {
    fn from(error: serde_json::Error) -> Self {
        Self::Json(error)
    }
}

pub fn print_result(
    mut writer: impl Write,
    format: Format,
    result: &CommandResult,
) -> Result<(), OutputError> {
    match format {
        Format::Json => print_json(&mut writer, &result.payload),
        Format::Text => print_text(&mut writer, &result.text),
    }
}

pub fn print_json<T: serde::Serialize + ?Sized>(
    writer: &mut impl Write,
    value: &T,
) -> Result<(), OutputError> {
    writeln!(writer, "{}", serde_json::to_string_pretty(value)?)?;
    Ok(())
}

pub fn print_text(writer: &mut impl Write, text: &str) -> Result<(), OutputError> {
    writeln!(writer, "{text}")?;
    Ok(())
}

pub fn print_status(mut writer: impl Write, message: &str) -> std::io::Result<()> {
    writeln!(writer, "gwiki: {message}")
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct SearchOutput {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub query: String,
    pub limit: usize,
    pub results: Vec<SearchResultOutput>,
    pub degradations: Vec<String>,
}

impl SearchOutput {
    pub fn new(
        scope: ScopeIdentity,
        query: impl Into<String>,
        limit: usize,
        results: Vec<SearchResultOutput>,
        degradations: Vec<String>,
    ) -> Self {
        Self {
            command: "search",
            scope,
            query: query.into(),
            limit,
            results,
            degradations,
        }
    }
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct SearchResultOutput {
    pub title: Option<String>,
    pub wiki_page: PathBuf,
    pub source_path: PathBuf,
    pub snippet: String,
    pub score: f64,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct QueryOutput {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub query: String,
    pub answer: String,
    pub citations: Vec<QueryCitation>,
}

impl QueryOutput {
    pub fn answered(
        scope: ScopeIdentity,
        query: impl Into<String>,
        answer: impl Into<String>,
        citations: Vec<QueryCitation>,
    ) -> Self {
        Self {
            command: "query",
            scope,
            query: query.into(),
            answer: answer.into(),
            citations,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct QueryCitation {
    pub source_path: PathBuf,
    pub wiki_page: PathBuf,
    pub title: Option<String>,
    pub lines: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct AuditOutput {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub unsupported_claim_count: usize,
    pub report_path: Option<PathBuf>,
    pub source_paths: Vec<PathBuf>,
}

impl AuditOutput {
    pub fn new(
        scope: ScopeIdentity,
        unsupported_claim_count: usize,
        report_path: Option<PathBuf>,
        source_paths: Vec<PathBuf>,
    ) -> Self {
        Self {
            command: "audit",
            scope,
            unsupported_claim_count,
            report_path,
            source_paths,
        }
    }
}

pub fn render_query_text(output: &QueryOutput) -> String {
    let mut text = format!(
        "Query answer\nScope: {}\nQuestion: {}\nAnswer: {}\n",
        output.scope, output.query, output.answer
    );
    if output.citations.is_empty() {
        text.push_str("Citations: none\n");
        return text;
    }

    text.push_str("Citations:\n");
    for citation in &output.citations {
        text.push_str("- Source: ");
        text.push_str(&citation.source_path.display().to_string());
        text.push_str(" | Wiki: ");
        text.push_str(&citation.wiki_page.display().to_string());
        if let Some(title) = &citation.title {
            text.push_str(" | Title: ");
            text.push_str(title);
        }
        if let Some(lines) = &citation.lines {
            text.push_str(" | Lines: ");
            text.push_str(lines);
        }
        text.push('\n');
    }
    text
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn json_output_is_stable() {
        let scope = ScopeIdentity::topic("rust");
        let search = SearchOutput::new(
            scope.clone(),
            "ownership",
            2,
            vec![SearchResultOutput {
                title: Some("Ownership".to_string()),
                wiki_page: "wiki/topics/ownership.md".into(),
                source_path: "raw/INDEX.md".into(),
                snippet: "Ownership rules move values.".to_string(),
                score: 0.91,
            }],
            vec!["semantic_unavailable".to_string()],
        );
        let query = QueryOutput::answered(
            scope.clone(),
            "How does ownership work?",
            "Ownership controls value moves.",
            vec![QueryCitation {
                source_path: "raw/INDEX.md".into(),
                wiki_page: "wiki/topics/ownership.md".into(),
                title: Some("Ownership".to_string()),
                lines: Some("4-8".to_string()),
            }],
        );
        let audit = AuditOutput::new(
            scope,
            1,
            Some("outputs/audit-20260529.json".into()),
            vec!["raw/INDEX.md".into()],
        );

        assert_eq!(
            serde_json::to_value(&search).expect("search JSON"),
            json!({
                "command": "search",
                "scope": {"kind": "topic", "id": "rust"},
                "query": "ownership",
                "limit": 2,
                "results": [{
                    "title": "Ownership",
                    "wiki_page": "wiki/topics/ownership.md",
                    "source_path": "raw/INDEX.md",
                    "snippet": "Ownership rules move values.",
                    "score": 0.91
                }],
                "degradations": ["semantic_unavailable"]
            })
        );
        assert_eq!(
            serde_json::to_value(&query).expect("query JSON"),
            json!({
                "command": "query",
                "scope": {"kind": "topic", "id": "rust"},
                "query": "How does ownership work?",
                "answer": "Ownership controls value moves.",
                "citations": [{
                    "source_path": "raw/INDEX.md",
                    "wiki_page": "wiki/topics/ownership.md",
                    "title": "Ownership",
                    "lines": "4-8"
                }]
            })
        );
        assert_eq!(
            serde_json::to_value(&audit).expect("audit JSON"),
            json!({
                "command": "audit",
                "scope": {"kind": "topic", "id": "rust"},
                "unsupported_claim_count": 1,
                "report_path": "outputs/audit-20260529.json",
                "source_paths": ["raw/INDEX.md"]
            })
        );
    }

    #[test]
    fn query_output_includes_citations() {
        let query = QueryOutput::answered(
            ScopeIdentity::project("project-123"),
            "Which page explains ownership?",
            "See the Ownership page.",
            vec![QueryCitation {
                source_path: "raw/rust-book.md".into(),
                wiki_page: "wiki/topics/ownership.md".into(),
                title: Some("Ownership".to_string()),
                lines: Some("12-21".to_string()),
            }],
        );

        let citation = query.citations.first().expect("citation");
        assert_eq!(
            citation.source_path,
            std::path::PathBuf::from("raw/rust-book.md")
        );
        assert_eq!(
            citation.wiki_page,
            std::path::PathBuf::from("wiki/topics/ownership.md")
        );

        let rendered = render_query_text(&query);
        assert!(rendered.contains("Source: raw/rust-book.md"));
        assert!(rendered.contains("Wiki: wiki/topics/ownership.md"));
    }
}
