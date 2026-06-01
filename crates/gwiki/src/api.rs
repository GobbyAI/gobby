use std::fmt;
use std::path::PathBuf;

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
    Search {
        query: String,
        scope: ScopeSelection,
        limit: usize,
        include_semantic: bool,
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
    Audit {
        scope: ScopeSelection,
    },
    Lint {
        scope: ScopeSelection,
    },
    Health {
        scope: ScopeSelection,
    },
    Status {
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
pub struct ScopeSelection {
    project: bool,
    topic: Option<String>,
}

impl ScopeSelection {
    pub fn global() -> Self {
        Self {
            project: false,
            topic: None,
        }
    }

    pub fn project() -> Self {
        Self {
            project: true,
            topic: None,
        }
    }

    pub fn topic(topic: impl Into<String>) -> Self {
        Self {
            project: false,
            topic: Some(topic.into()),
        }
    }

    pub fn identity(&self) -> ScopeIdentity {
        if let Some(topic) = &self.topic {
            return ScopeIdentity::topic(topic.clone());
        }

        if self.project {
            ScopeIdentity::project("current")
        } else {
            ScopeIdentity::global()
        }
    }

    pub fn is_project(&self) -> bool {
        self.project
    }

    pub fn topic_name(&self) -> Option<&str> {
        self.topic.as_deref()
    }
}

impl Default for ScopeSelection {
    fn default() -> Self {
        Self::global()
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
        let global = ScopeSelection::global();
        assert!(!global.is_project());
        assert_eq!(global.topic_name(), None);
        assert_eq!(ScopeSelection::default(), global);
        assert_eq!(global.identity(), crate::ScopeIdentity::global());

        let project = ScopeSelection::project();
        assert!(project.is_project());
        assert_eq!(project.topic_name(), None);
        assert_eq!(project.identity(), crate::ScopeIdentity::project("current"));

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
