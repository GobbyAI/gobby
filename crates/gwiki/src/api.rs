use std::fmt;
use std::path::{Path, PathBuf};

use gobby_core::ai_context::AiContext;
use gobby_core::config::AiRouting;

use crate::{exports, synthesis};

/// Parsed gwiki command passed in from the binary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Init {
        scope: ScopeSelection,
    },
    Index {
        scope: ScopeSelection,
        force: bool,
    },
    Collect {
        scope: ScopeSelection,
    },
    Code(crate::commands::code::CodeCommandOptions),
    IngestFile {
        path: PathBuf,
        scope: ScopeSelection,
        options: IngestFileOptions,
    },
    IngestUrl {
        urls: Vec<String>,
        scope: ScopeSelection,
        max_age_hours: u64,
    },
    SyncSessions {
        scope: ScopeSelection,
        options: SyncSessionsOptions,
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
    Purge {
        target: PurgeTarget,
        yes: bool,
    },
    Prune {
        force: bool,
    },
    Search {
        query: String,
        scope: ScopeSelection,
        limit: usize,
        include_semantic: bool,
        token_budget: Option<usize>,
        /// Opt-in: also return quarantined candidate pages (#17727), for the
        /// librarian/upkeep loops. Default retrieval excludes candidates.
        include_candidates: bool,
    },
    Read {
        target: ReadTarget,
        scope: ScopeSelection,
    },
    Pages {
        scope: ScopeSelection,
        /// Restrict the listing to wiki paths starting with this prefix.
        prefix: Option<String>,
    },
    PageWrite {
        scope: ScopeSelection,
        /// Vault-relative markdown path under `knowledge/**`.
        path: String,
        mode: PageWriteMode,
        /// SHA-256 hex precondition against the current on-disk bytes.
        expected_hash: Option<String>,
    },
    PageDelete {
        scope: ScopeSelection,
        /// Vault-relative markdown path under `knowledge/**`.
        path: String,
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
    Compile {
        topic: Option<String>,
        outline: Vec<String>,
        source: Vec<String>,
        target_kind: synthesis::ArticleKind,
        target_page: Option<PathBuf>,
        write_intent: bool,
        ai: AiRouting,
        scope: ScopeSelection,
    },
    Export {
        scope: ScopeSelection,
        command: exports::ExportCommand,
    },
    Graph {
        scope: ScopeSelection,
        options: GraphCommandOptions,
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
    Normalize {
        scope: ScopeSelection,
        check: bool,
    },
    Health {
        scope: ScopeSelection,
    },
    Librarian {
        scope: ScopeSelection,
        ai: AiRouting,
    },
    Upkeep {
        scope: ScopeSelection,
        options: UpkeepOptions,
        ai: AiRouting,
    },
    Recap {
        scope: ScopeSelection,
        options: RecapOptions,
        ai: AiRouting,
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

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RunOptions {
    pub quiet: bool,
}

/// Budgets and toggles for the `upkeep` conductor.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UpkeepOptions {
    pub max_pages: usize,
    pub min_mentions: usize,
    pub max_sources_per_page: usize,
    pub dry_run: bool,
    pub time_budget_seconds: Option<u64>,
}

impl UpkeepOptions {
    pub const DEFAULT_MAX_PAGES: usize = crate::upkeep::DEFAULT_MAX_PAGES;
    pub const DEFAULT_MIN_MENTIONS: usize = crate::upkeep::DEFAULT_MIN_MENTIONS;
    pub const DEFAULT_MAX_SOURCES_PER_PAGE: usize = crate::upkeep::DEFAULT_MAX_SOURCES_PER_PAGE;
}

impl Default for UpkeepOptions {
    fn default() -> Self {
        Self {
            max_pages: Self::DEFAULT_MAX_PAGES,
            min_mentions: Self::DEFAULT_MIN_MENTIONS,
            max_sources_per_page: Self::DEFAULT_MAX_SOURCES_PER_PAGE,
            dry_run: false,
            time_budget_seconds: None,
        }
    }
}

/// Options for the `recap` daily session page.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RecapOptions {
    /// Target day as `YYYY-MM-DD` (UTC attribution); `None` means today.
    pub date: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadTarget {
    Path(PathBuf),
    Title(String),
}

/// Write mode for `gwiki page write`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PageWriteMode {
    /// Create the page or overwrite the existing revision.
    Upsert,
    /// Fail with a distinct error if the page already exists.
    Create,
}

/// Behavior flags for `gwiki graph`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct GraphCommandOptions {
    /// Emit the graph JSON envelope on stdout instead of writing artifacts.
    pub stdout: bool,
    /// Restrict exported facts to knowledge or code surfaces.
    pub include: crate::graph::GraphInclude,
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
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncSessionsOptions {
    pub archive_dir: Option<PathBuf>,
    pub wiki_dir: Option<PathBuf>,
    pub limit: Option<usize>,
    pub raw: bool,
    /// Generate a daemon-equivalent summary for raw archives that have no daemon
    /// synthesis, instead of the structural skeleton. Degrades to skeleton when
    /// AI is unavailable.
    pub summarize: bool,
    /// Enrich daemon-synthesized session pages with connection links before write.
    pub enrich: bool,
}

impl Default for SyncSessionsOptions {
    fn default() -> Self {
        Self {
            archive_dir: None,
            wiki_dir: None,
            limit: None,
            raw: false,
            summarize: false,
            enrich: true,
        }
    }
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
        if self.no_ai {
            context.bindings.embed.routing = AiRouting::Off;
            context.bindings.audio_transcribe.routing = AiRouting::Off;
            context.bindings.audio_translate.routing = AiRouting::Off;
            context.bindings.vision_extract.routing = AiRouting::Off;
            context.bindings.text_generate.routing = AiRouting::Off;
            return;
        }

        if self.translate
            && let Some(target_lang) = &self.target_lang
        {
            context.bindings.audio_translate.target_lang = Some(target_lang.clone());
        }
    }
}

/// Shared scope flags accepted by shell commands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScopeSelection {
    Detect,
    ProjectRoot(PathBuf),
    Topic(String),
}

/// Scope target accepted by destructive purge commands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PurgeTarget {
    Selection(ScopeSelection),
    ProjectId(String),
}

impl PurgeTarget {
    pub fn selection(selection: ScopeSelection) -> Self {
        Self::Selection(selection)
    }

    pub fn project_id(project_id: impl Into<String>) -> Self {
        Self::ProjectId(project_id.into())
    }
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
    fn no_ai_forces_every_capability_off() {
        let mut source = EnvOnlySource;
        let mut context = AiContext::resolve(None, &mut source);
        IngestFileOptions {
            no_ai: true,
            ..IngestFileOptions::default()
        }
        .apply_to_ai_context(&mut context);
        assert_eq!(context.bindings.embed.routing, AiRouting::Off);
        assert_eq!(context.bindings.audio_transcribe.routing, AiRouting::Off);
        assert_eq!(context.bindings.audio_translate.routing, AiRouting::Off);
        assert_eq!(context.bindings.vision_extract.routing, AiRouting::Off);
        assert_eq!(context.bindings.text_generate.routing, AiRouting::Off);
    }

    #[test]
    fn dependency_direction_is_one_way() {
        fn dependency_tables(manifest: &toml::Value) -> Vec<&toml::map::Map<String, toml::Value>> {
            const TABLE_NAMES: [&str; 3] =
                ["dependencies", "dev-dependencies", "build-dependencies"];

            let root = manifest.as_table().expect("manifest root is a table");
            let mut tables = TABLE_NAMES
                .iter()
                .filter_map(|name| root.get(*name).and_then(toml::Value::as_table))
                .collect::<Vec<_>>();
            if let Some(targets) = root.get("target").and_then(toml::Value::as_table) {
                for target in targets.values().filter_map(toml::Value::as_table) {
                    tables.extend(
                        TABLE_NAMES
                            .iter()
                            .filter_map(|name| target.get(*name).and_then(toml::Value::as_table)),
                    );
                }
            }
            tables
        }

        fn declares_dependency(manifest: &toml::Value, dependency: &str) -> bool {
            dependency_tables(manifest)
                .iter()
                .any(|table| table.contains_key(dependency))
        }

        let manifest = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
            .expect("manifest is readable");
        let manifest: toml::Value = toml::from_str(&manifest).expect("manifest is valid TOML");

        assert!(
            declares_dependency(&manifest, "gobby-code"),
            "gobby-wiki must depend on gobby-code"
        );

        let gcode_manifest =
            std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../gcode/Cargo.toml"))
                .expect("gcode manifest is readable");
        let gcode_manifest: toml::Value =
            toml::from_str(&gcode_manifest).expect("gcode manifest is valid TOML");
        assert!(
            !declares_dependency(&gcode_manifest, "gobby-wiki"),
            "gobby-code must not depend on gobby-wiki"
        );
    }
}
