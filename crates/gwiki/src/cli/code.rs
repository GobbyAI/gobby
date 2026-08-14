use std::path::PathBuf;

use clap::{Args, ValueEnum};
use gobby_core::config::{AiRouting, FeatureCandidate};
use gobby_wiki::{
    AiDepth, CodeCommandOptions, DEFAULT_CODE_GRAPH_EDGE_LIMIT, ProseDepth, VerifyScope,
};

const MAX_POSITIVE_USIZE_ARG: usize = 1_000_000_000;

#[derive(Args, Debug)]
pub(super) struct CodeArgs {
    /// Output directory for generated Markdown docs.
    #[arg(long)]
    out: Option<String>,
    /// Remove generated CodeWiki output/cache under --out and exit.
    #[arg(
        long,
        conflicts_with_all = [
            "scope",
            "complete_scope",
            "no_ai",
            "ai_depth",
            "ai_aggregate_profile",
            "ai_aggregate_candidate",
            "ai_verify_profile",
            "ai_verify_scope",
            "ai_prose_depth",
            "ai_register",
            "edge_limit",
            "include_docs",
            "since",
            "max_workers",
            "repair_citations",
            "compare_to",
        ]
    )]
    purge: bool,
    /// Confirm destructive CodeWiki output purge.
    #[arg(long, requires = "purge")]
    force: bool,
    /// Limit docs to indexed files under one or more project-relative or absolute paths.
    /// Absolute paths must resolve inside the selected project root.
    #[arg(long, num_args = 1.., value_name = "PATH")]
    scope: Vec<String>,
    /// Treat --scope paths as the complete publication boundary.
    #[arg(long)]
    complete_scope: bool,
    /// Disable daemon-backed generation for this invocation.
    #[arg(long)]
    no_ai: bool,
    /// AI prose depth.
    #[arg(long, value_enum, default_value_t = AiDepthArg::Files)]
    ai_depth: AiDepthArg,
    /// Daemon feature profile for aggregate docs.
    #[arg(long, value_name = "PROFILE")]
    ai_aggregate_profile: Option<String>,
    /// Pin aggregate generation to an ordered provider/model candidate chain.
    #[arg(
        long,
        value_name = "PROVIDER/MODEL[@EFFORT]",
        value_parser = FeatureCandidate::parse_cli_label,
        conflicts_with = "ai_aggregate_profile"
    )]
    ai_aggregate_candidate: Vec<FeatureCandidate>,
    /// Daemon feature profile for grounded verification.
    #[arg(long, value_name = "PROFILE")]
    ai_verify_profile: Option<String>,
    /// Pages that run grounded verification.
    #[arg(long, value_enum, default_value_t = AiVerifyScopeArg::Aggregates)]
    ai_verify_scope: AiVerifyScopeArg,
    /// Per-page prose token budget.
    #[arg(long, value_enum, default_value_t = AiProseDepthArg::Standard)]
    ai_prose_depth: AiProseDepthArg,
    /// Audience register for generated prose.
    #[arg(long, value_enum)]
    ai_register: Option<AiRegisterArg>,
    /// Maximum graph edges to fetch.
    #[arg(
        long,
        default_value_t = DEFAULT_CODE_GRAPH_EDGE_LIMIT,
        value_parser = parse_positive_usize
    )]
    edge_limit: usize,
    /// Include narrative content files alongside code and structured config.
    #[arg(long)]
    include_docs: bool,
    /// Regenerate pages affected by changes since this Git ref.
    #[arg(long, value_name = "GIT_REF")]
    since: Option<String>,
    /// Compare current metadata with a Git snapshot.
    #[arg(
        long,
        value_name = "GIT_REF[:META_PATH]",
        conflicts_with_all = [
            "purge",
            "force",
            "scope",
            "complete_scope",
            "no_ai",
            "ai_depth",
            "ai_aggregate_profile",
            "ai_aggregate_candidate",
            "ai_verify_profile",
            "ai_verify_scope",
            "ai_prose_depth",
            "ai_register",
            "edge_limit",
            "include_docs",
            "since",
            "max_workers",
            "repair_citations",
        ]
    )]
    compare_to: Option<String>,
    /// Bounded worker pool for file-page generation.
    #[arg(long, default_value_t = 1, value_name = "N", value_parser = parse_positive_usize)]
    max_workers: usize,
    /// Re-anchor existing citations without generating content.
    #[arg(long)]
    repair_citations: bool,
    /// Allow stale index data by skipping the generation-path freshness check.
    #[arg(long)]
    allow_stale: bool,
}

impl CodeArgs {
    pub(super) fn into_options(
        self,
        project_root: PathBuf,
        quiet: bool,
        verbose: bool,
    ) -> CodeCommandOptions {
        CodeCommandOptions {
            project_root,
            out: self.out,
            purge: self.purge,
            force: self.force,
            scope: self.scope,
            complete_scope: self.complete_scope,
            ai: self.no_ai.then_some(AiRouting::Off),
            ai_depth: self.ai_depth.into(),
            ai_prose_depth: self.ai_prose_depth.into(),
            ai_register: self.ai_register.map(AiRegisterArg::label),
            ai_aggregate_profile: self.ai_aggregate_profile,
            ai_aggregate_candidates: self.ai_aggregate_candidate,
            ai_verify_profile: self.ai_verify_profile,
            ai_verify_scope: self.ai_verify_scope.into(),
            edge_limit: self.edge_limit,
            include_docs: self.include_docs,
            since: self.since,
            compare_to: self.compare_to,
            max_workers: self.max_workers,
            repair_citations: self.repair_citations,
            allow_stale: self.allow_stale,
            quiet,
            verbose,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, ValueEnum)]
enum AiDepthArg {
    Sections,
    #[default]
    Files,
    Symbols,
}

impl From<AiDepthArg> for AiDepth {
    fn from(value: AiDepthArg) -> Self {
        match value {
            AiDepthArg::Sections => Self::Sections,
            AiDepthArg::Files => Self::Files,
            AiDepthArg::Symbols => Self::Symbols,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, ValueEnum)]
enum AiProseDepthArg {
    Brief,
    #[default]
    Standard,
    Deep,
}

impl From<AiProseDepthArg> for ProseDepth {
    fn from(value: AiProseDepthArg) -> Self {
        match value {
            AiProseDepthArg::Brief => Self::Brief,
            AiProseDepthArg::Standard => Self::Standard,
            AiProseDepthArg::Deep => Self::Deep,
        }
    }
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum AiRegisterArg {
    Newcomer,
    Maintainer,
    Agent,
}

impl AiRegisterArg {
    fn label(self) -> String {
        match self {
            Self::Newcomer => "newcomer",
            Self::Maintainer => "maintainer",
            Self::Agent => "agent",
        }
        .to_string()
    }
}

#[derive(Clone, Copy, Debug, Default, ValueEnum)]
enum AiVerifyScopeArg {
    #[default]
    Aggregates,
    All,
}

impl From<AiVerifyScopeArg> for VerifyScope {
    fn from(value: AiVerifyScopeArg) -> Self {
        match value {
            AiVerifyScopeArg::Aggregates => Self::Aggregates,
            AiVerifyScopeArg::All => Self::All,
        }
    }
}

fn parse_positive_usize(value: &str) -> Result<usize, String> {
    let parsed = value
        .parse::<usize>()
        .map_err(|_| format!("value '{value}' must be a positive integer"))?;
    if parsed == 0 {
        Err(format!("value '{value}' must be a positive integer"))
    } else if parsed > MAX_POSITIVE_USIZE_ARG {
        Err(format!(
            "value must be no more than {MAX_POSITIVE_USIZE_ARG}"
        ))
    } else {
        Ok(parsed)
    }
}
