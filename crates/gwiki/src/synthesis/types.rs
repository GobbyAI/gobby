use std::path::PathBuf;

use serde::Serialize;

use crate::explainer::ExplainerReport;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ArticleKind {
    Source,
    Concept,
    Topic,
}

impl ArticleKind {
    pub fn directory(self) -> &'static str {
        match self {
            Self::Source => "knowledge/sources",
            Self::Concept => "knowledge/concepts",
            Self::Topic => "knowledge/topics",
        }
    }

    pub fn source_kind(self) -> &'static str {
        match self {
            Self::Source => "source_note",
            Self::Concept => "concept",
            Self::Topic => "topic",
        }
    }

    /// Slug suffix applied when a page title collides with an agent
    /// instruction filename (`Claude` -> `claude-concept`, see #17645).
    pub fn reserved_suffix(self) -> &'static str {
        match self {
            Self::Source => "source",
            Self::Concept => "concept",
            Self::Topic => "topic",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SynthesisSource {
    pub title: String,
    pub path: PathBuf,
    pub chunks: Vec<String>,
    /// Manifest-backed digest page already on disk for this source
    /// (`knowledge/sources/<id>.md`). When set, synthesis links the article to
    /// this page instead of writing a new title-slugged stub.
    pub existing_page: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SynthesisInput {
    pub handoff_id: String,
    pub topic: String,
    pub outline: Vec<String>,
    pub target_kind: ArticleKind,
    pub accepted_sources: Vec<SynthesisSource>,
    pub citations: Vec<String>,
    pub conflicting_claims: Vec<String>,
    pub missing_evidence: Vec<String>,
    /// Body of the target page when recompiling an existing article: the
    /// explainer prompt carries it so the model updates the page instead of
    /// regenerating it from scratch (update-over-create).
    pub existing_page_body: Option<String>,
    /// Alternate titles rendered as frontmatter `aliases` (observed case
    /// variants of an entity name).
    pub aliases: Vec<String>,
    /// Frontmatter tags appended after the standard `gwiki`/`compiled` pair
    /// (e.g. the `entity` marker on entity concept pages).
    pub extra_tags: Vec<String>,
    /// Quarantine the synthesized article as an untrusted LLM-proposed page
    /// (#17727): frontmatter carries `candidate: true` until promotion.
    pub candidate: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SynthesisPrompt {
    pub system: String,
    pub user: String,
    pub daemon_synthesis_available: bool,
    pub tokens_estimated: usize,
    pub truncated_sources: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SynthesizedPage {
    pub path: PathBuf,
    pub title: String,
    pub markdown: String,
    /// Explainer accounting for the compiled article; `None` on source pages.
    pub explainer: Option<ExplainerReport>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WritePolicy {
    RequireMergeIntent,
    AllowOverwriteAfterMerge,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PageWriteKind {
    Created,
    Overwritten,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PageWriteOutcome {
    pub path: PathBuf,
    pub kind: PageWriteKind,
}
