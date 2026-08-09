use std::path::PathBuf;

use clap::{Args, ValueEnum};
use gobby_core::config::{AiRouting, FeatureCandidate};
use gobby_wiki::commands::code::{
    AiDepth, CodeCommandOptions, CodewikiAiOptions, DEFAULT_CODE_GRAPH_EDGE_LIMIT, ProseDepth,
    ProseRegister, VerifyScope,
};
use gobby_wiki::output::Format;

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
            "ai",
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
    /// Limit docs to indexed files under one or more paths.
    #[arg(long, num_args = 1.., value_name = "PATH")]
    scope: Vec<String>,
    /// Treat --scope paths as the complete publication boundary.
    #[arg(long)]
    complete_scope: bool,
    /// Override AI routing for generated summaries.
    #[arg(long, value_name = "auto|daemon|direct|off")]
    ai: Option<AiRouting>,
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
            "ai",
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
    /// Skip the generation-path code index freshness check.
    #[arg(long)]
    no_freshness: bool,
}

impl CodeArgs {
    pub(super) fn into_options(
        self,
        project_root: PathBuf,
        format: Format,
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
            ai: CodewikiAiOptions {
                routing: self.ai,
                depth: self.ai_depth.into(),
                prose_depth: self.ai_prose_depth.into(),
                register: self.ai_register.map(Into::into),
                aggregate_profile: self.ai_aggregate_profile,
                aggregate_candidates: self.ai_aggregate_candidate,
                verify_profile: self.ai_verify_profile,
                verify_model: None,
                verify_api_key: None,
                verify_scope: self.ai_verify_scope.into(),
            },
            edge_limit: self.edge_limit,
            include_docs: self.include_docs,
            since: self.since,
            compare_to: self.compare_to,
            max_workers: self.max_workers,
            repair_citations: self.repair_citations,
            no_freshness: self.no_freshness,
            format,
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

impl From<AiRegisterArg> for ProseRegister {
    fn from(value: AiRegisterArg) -> Self {
        match value {
            AiRegisterArg::Newcomer => Self::Newcomer,
            AiRegisterArg::Maintainer => Self::Maintainer,
            AiRegisterArg::Agent => Self::Agent,
        }
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
        .map_err(|_| "value must be a positive integer".to_string())?;
    if parsed == 0 {
        Err("value must be a positive integer".to_string())
    } else if parsed > MAX_POSITIVE_USIZE_ARG {
        Err(format!(
            "value must be no more than {MAX_POSITIVE_USIZE_ARG}"
        ))
    } else {
        Ok(parsed)
    }
}
