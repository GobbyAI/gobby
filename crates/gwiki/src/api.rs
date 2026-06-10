use std::fmt;
use std::path::{Path, PathBuf};

use gobby_core::ai_context::AiContext;
use gobby_core::config::AiRouting;

use crate::{exports, research, synthesis};

/// Parsed gwiki command passed in from the binary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Init {
        scope: ScopeSelection,
    },
    Setup {
        scope: ScopeSelection,
        options: SetupOptions,
    },
    Index {
        scope: ScopeSelection,
    },
    Collect {
        scope: ScopeSelection,
    },
    IngestFile {
        path: PathBuf,
        scope: ScopeSelection,
        options: IngestFileOptions,
    },
    IngestUrl {
        urls: Vec<String>,
        scope: ScopeSelection,
    },
    Refresh {
        scope: ScopeSelection,
        source_ids: Vec<String>,
        dry_run: bool,
    },
    Sources {
        scope: ScopeSelection,
    },
    RemoveSource {
        id: String,
        scope: ScopeSelection,
        dry_run: bool,
        keep_asset: bool,
    },
    Search {
        query: String,
        scope: ScopeSelection,
        limit: usize,
        include_semantic: bool,
    },
    Ask {
        query: String,
        scope: ScopeSelection,
        llm: bool,
        ai: AiRouting,
        require_ai: bool,
    },
    Read {
        target: ReadTarget,
        scope: ScopeSelection,
    },
    Backlinks {
        page: String,
        scope: ScopeSelection,
    },
    LinkSuggest {
        scope: ScopeSelection,
        limit: usize,
    },
    Benchmark {
        scope: ScopeSelection,
        options: BenchmarkOptions,
    },
    Research(research::ResearchOptions),
    Compile {
        topic: Option<String>,
        outline: Vec<String>,
        target_kind: synthesis::ArticleKind,
        target_page: Option<PathBuf>,
        write_intent: bool,
        scope: ScopeSelection,
    },
    Export {
        scope: ScopeSelection,
        command: exports::ExportCommand,
    },
    Graph {
        scope: ScopeSelection,
    },
    GraphContext {
        scope: ScopeSelection,
    },
    ReviewReport {
        scope: ScopeSelection,
        options: ReviewReportOptions,
    },
    Audit {
        scope: ScopeSelection,
    },
    Lint {
        scope: ScopeSelection,
    },
    Health {
        scope: ScopeSelection,
    },
    Librarian {
        scope: ScopeSelection,
    },
    Status {
        scope: ScopeSelection,
    },
    Trust {
        scope: ScopeSelection,
    },
    CitationQuality {
        scope: ScopeSelection,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadTarget {
    Path(PathBuf),
    Title(String),
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SetupOptions {
    pub standalone: bool,
    pub database_url: Option<String>,
    pub no_services: bool,
    pub falkordb_host: Option<String>,
    pub falkordb_port: Option<u16>,
    pub falkordb_password: Option<String>,
    pub qdrant_url: Option<String>,
    pub embedding_provider: Option<String>,
    pub embedding_api_base: Option<String>,
    pub embedding_model: Option<String>,
    pub embedding_query_prefix: Option<String>,
    pub embedding_vector_dim: Option<usize>,
    pub embedding_api_key: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BenchmarkOptions {
    pub retrieval_candidates: usize,
}

impl BenchmarkOptions {
    pub const DEFAULT_RETRIEVAL_CANDIDATES: usize =
        crate::benchmark::DEFAULT_RETRIEVAL_PRECISION_CANDIDATES;
}

impl Default for BenchmarkOptions {
    fn default() -> Self {
        Self {
            retrieval_candidates: Self::DEFAULT_RETRIEVAL_CANDIDATES,
        }
    }
}

/// AI and media policy options for `ingest-file`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct IngestFileOptions {
    pub no_ai: bool,
    pub translate: bool,
    pub target_lang: Option<String>,
    pub video_frame_interval_seconds: Option<u32>,
    pub transcription_routing: Option<AiRouting>,
    pub vision_routing: Option<AiRouting>,
    pub text_routing: Option<AiRouting>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReviewReportOptions {
    pub files: Vec<String>,
    pub symbols: Vec<String>,
    pub diff_path: Option<PathBuf>,
    pub output: String,
}

impl IngestFileOptions {
    pub fn apply_to_ai_context(&self, context: &mut AiContext) {
        if !self.no_ai {
            if let Some(routing) = self.transcription_routing {
                if self.translate {
                    context.bindings.audio_translate.routing = routing;
                } else {
                    context.bindings.audio_transcribe.routing = routing;
                }
            }
            if let Some(routing) = self.vision_routing {
                context.bindings.vision_extract.routing = routing;
            }
            if let Some(routing) = self.text_routing {
                context.bindings.text_generate.routing = routing;
            }
            if self.translate
                && let Some(target_lang) = &self.target_lang
            {
                context.bindings.audio_translate.target_lang = Some(target_lang.clone());
            }
            return;
        }

        context.bindings.embed.routing = AiRouting::Off;
        context.bindings.audio_transcribe.routing = AiRouting::Off;
        context.bindings.audio_translate.routing = AiRouting::Off;
        context.bindings.vision_extract.routing = AiRouting::Off;
        context.bindings.text_generate.routing = AiRouting::Off;
    }
}

/// Shared scope flags accepted by shell commands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScopeSelection {
    Detect,
    ProjectRoot(PathBuf),
    Topic(String),
}

impl ScopeSelection {
    pub fn detect() -> Self {
        Self::Detect
    }

    pub fn project(root: impl Into<PathBuf>) -> Self {
        Self::ProjectRoot(root.into())
    }

    pub fn topic(topic: impl Into<String>) -> Self {
        Self::Topic(topic.into())
    }

    pub fn identity(&self) -> ScopeIdentity {
        match self {
            Self::Detect => ScopeIdentity::global(),
            Self::ProjectRoot(root) => ScopeIdentity::project(root.display().to_string()),
            Self::Topic(topic) => ScopeIdentity::topic(topic.clone()),
        }
    }

    pub fn is_project(&self) -> bool {
        matches!(self, Self::ProjectRoot(_))
    }

    pub fn project_root(&self) -> Option<&Path> {
        match self {
            Self::ProjectRoot(root) => Some(root.as_path()),
            Self::Detect | Self::Topic(_) => None,
        }
    }

    pub fn topic_name(&self) -> Option<&str> {
        match self {
            Self::Topic(topic) => Some(topic.as_str()),
            Self::Detect | Self::ProjectRoot(_) => None,
        }
    }
}

impl Default for ScopeSelection {
    fn default() -> Self {
        Self::detect()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ScopeKind {
    Global,
    Project,
    Topic,
}

impl ScopeKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Global => "global",
            Self::Project => "project",
            Self::Topic => "topic",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ScopeIdentity {
    pub kind: ScopeKind,
    pub id: String,
}

impl ScopeIdentity {
    pub fn global() -> Self {
        Self {
            kind: ScopeKind::Global,
            id: "default".to_string(),
        }
    }

    pub fn project(id: impl Into<String>) -> Self {
        Self {
            kind: ScopeKind::Project,
            id: id.into(),
        }
    }

    pub fn topic(id: impl Into<String>) -> Self {
        Self {
            kind: ScopeKind::Topic,
            id: id.into(),
        }
    }
}

impl fmt::Display for ScopeIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.kind.as_str(), self.id)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CommandOutcome {
    pub status_messages: Vec<String>,
    pub result: CommandResult,
    pub exit_code: u8,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CommandResult {
    pub payload: serde_json::Value,
    pub text: String,
}

#[cfg(test)]
mod tests {
    use super::{IngestFileOptions, ScopeSelection};
    use gobby_core::ai_context::AiContext;
    use gobby_core::config::{AiRouting, EnvOnlySource};

    #[test]
    fn scope_selection_constructors_express_allowed_states() {
        let detect = ScopeSelection::detect();
        assert!(!detect.is_project());
        assert_eq!(detect.topic_name(), None);
        assert_eq!(ScopeSelection::default(), detect);
        assert_eq!(detect.identity(), crate::ScopeIdentity::global());

        let project = ScopeSelection::project("/repo");
        assert!(project.is_project());
        assert_eq!(project.topic_name(), None);
        assert_eq!(project.project_root(), Some(std::path::Path::new("/repo")));
        assert_eq!(project.identity(), crate::ScopeIdentity::project("/repo"));

        let topic = ScopeSelection::topic("ops");
        assert!(!topic.is_project());
        assert_eq!(topic.topic_name(), Some("ops"));
    }

    #[test]
    fn target_lang_requires_translate_flag() {
        let mut source = EnvOnlySource;
        let mut context = AiContext::resolve(None, &mut source);

        IngestFileOptions {
            target_lang: Some("fr".to_string()),
            ..IngestFileOptions::default()
        }
        .apply_to_ai_context(&mut context);
        assert!(context.bindings.audio_translate.target_lang.is_none());

        IngestFileOptions {
            translate: true,
            target_lang: Some("fr".to_string()),
            ..IngestFileOptions::default()
        }
        .apply_to_ai_context(&mut context);
        assert_eq!(
            context.bindings.audio_translate.target_lang.as_deref(),
            Some("fr")
        );
    }

    #[test]
    fn transcription_routing_applies_to_active_audio_capability() {
        let mut source = EnvOnlySource;
        let mut context = AiContext::resolve(None, &mut source);
        let original_translate_route = context.bindings.audio_translate.routing;

        IngestFileOptions {
            transcription_routing: Some(AiRouting::Direct),
            ..IngestFileOptions::default()
        }
        .apply_to_ai_context(&mut context);
        assert_eq!(context.bindings.audio_transcribe.routing, AiRouting::Direct);
        assert_eq!(
            context.bindings.audio_translate.routing,
            original_translate_route
        );

        let mut source = EnvOnlySource;
        let mut context = AiContext::resolve(None, &mut source);
        let original_transcribe_route = context.bindings.audio_transcribe.routing;
        IngestFileOptions {
            translate: true,
            transcription_routing: Some(AiRouting::Direct),
            ..IngestFileOptions::default()
        }
        .apply_to_ai_context(&mut context);
        assert_eq!(
            context.bindings.audio_transcribe.routing,
            original_transcribe_route
        );
        assert_eq!(context.bindings.audio_translate.routing, AiRouting::Direct);
    }

    #[test]
    fn crate_has_no_gcode_dependency() {
        let manifest = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
            .expect("manifest is readable");
        let manifest: toml::Value = toml::from_str(&manifest).expect("manifest is valid TOML");
        let dependencies = manifest
            .get("dependencies")
            .and_then(toml::Value::as_table)
            .expect("manifest has dependencies table");

        assert!(
            dependencies.contains_key("gobby-core"),
            "gobby-wiki must depend on gobby-core"
        );
        assert!(
            !dependencies.contains_key("gobby-code"),
            "gobby-wiki must not depend on gobby-code"
        );
    }
}
