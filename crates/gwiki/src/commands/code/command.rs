use std::collections::BTreeSet;
use std::path::PathBuf;

use gobby_code::codewiki_facts::{CodewikiFacts, FreshnessStatus, ensure_project_fresh};
use gobby_core::ai::generation::{
    GenerationTier, profile_for_tier, resolve_direct_generation_target,
};
use gobby_core::ai_context::AiContext;
use gobby_core::config::{AiRouting, FeatureCandidate};

use crate::{CommandOutcome, ScopeIdentity, WikiError};

use super::compare::{compare_summary_text, compare_to};
use super::purge::{purge_confirmation, purge_summary, purge_summary_text};
use super::run::{repair_summary, repair_summary_text, run_summary, run_summary_text};
use super::{
    AiDepth, CodeEngineRuntime, CodewikiAiOptions, ProseDepth, ProseRegister, VerifyScope,
};

pub const DEFAULT_CODE_GRAPH_EDGE_LIMIT: usize = 5_000;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CodeCommandOptions {
    pub project_root: PathBuf,
    pub out: Option<String>,
    pub purge: bool,
    pub force: bool,
    pub scope: Vec<String>,
    pub complete_scope: bool,
    pub ai: Option<AiRouting>,
    pub ai_depth: AiDepth,
    pub ai_prose_depth: ProseDepth,
    pub ai_register: Option<String>,
    pub ai_aggregate_profile: Option<String>,
    pub ai_aggregate_candidates: Vec<FeatureCandidate>,
    pub ai_verify_profile: Option<String>,
    pub ai_verify_scope: VerifyScope,
    pub edge_limit: usize,
    pub include_docs: bool,
    pub since: Option<String>,
    pub compare_to: Option<String>,
    pub max_workers: usize,
    pub repair_citations: bool,
    pub allow_stale: bool,
    pub quiet: bool,
    pub verbose: bool,
}

impl CodeCommandOptions {
    pub fn requires_freshness(&self) -> bool {
        self.compare_to.is_none() && !self.purge && !self.repair_citations
    }

    fn ai_options(&self) -> Result<CodewikiAiOptions, WikiError> {
        let register = self
            .ai_register
            .as_deref()
            .map(parse_prose_register)
            .transpose()?;
        Ok(CodewikiAiOptions {
            routing: self.ai,
            depth: self.ai_depth,
            prose_depth: self.ai_prose_depth,
            register,
            aggregate_profile: self.ai_aggregate_profile.clone(),
            aggregate_candidates: self.ai_aggregate_candidates.clone(),
            verify_profile: self.ai_verify_profile.clone(),
            verify_model: None,
            verify_api_key: None,
            verify_scope: self.ai_verify_scope,
        })
    }
}

#[derive(Debug)]
pub(crate) enum CodeCommandError {
    Wiki(WikiError),
    Freshness(anyhow::Error),
    Command {
        action: &'static str,
        path: Option<PathBuf>,
        source: anyhow::Error,
    },
}

impl CodeCommandError {
    fn command(action: &'static str, path: Option<PathBuf>, source: anyhow::Error) -> Self {
        Self::Command {
            action,
            path,
            source,
        }
    }
}

impl From<WikiError> for CodeCommandError {
    fn from(error: WikiError) -> Self {
        Self::Wiki(error)
    }
}

impl From<CodeCommandError> for WikiError {
    fn from(error: CodeCommandError) -> Self {
        match error {
            CodeCommandError::Wiki(error) => error,
            CodeCommandError::Freshness(source) => Self::Freshness {
                detail: format!("{source:#}"),
            },
            CodeCommandError::Command {
                action,
                path,
                source,
            } => match source.downcast::<WikiError>() {
                Ok(error) => error,
                Err(source) => match source.downcast::<std::io::Error>() {
                    Ok(source) => Self::Io {
                        action,
                        path,
                        source,
                    },
                    Err(source) => Self::Generation {
                        detail: format!("{source:#}"),
                    },
                },
            },
        }
    }
}

fn parse_prose_register(value: &str) -> Result<ProseRegister, WikiError> {
    match value {
        "newcomer" => Ok(ProseRegister::Newcomer),
        "maintainer" => Ok(ProseRegister::Maintainer),
        "agent" => Ok(ProseRegister::Agent),
        _ => Err(WikiError::InvalidInput {
            field: "ai-register",
            message: format!("unsupported register '{value}'"),
        }),
    }
}

pub(crate) fn run_command(options: CodeCommandOptions) -> Result<CommandOutcome, CodeCommandError> {
    let ai = options.ai_options()?;
    let compare_scope = ScopeIdentity::project(options.project_root.display().to_string());
    if let Some(base_ref) = options.compare_to.as_deref() {
        let summary = compare_to(&options.project_root, options.out.as_deref(), base_ref).map_err(
            |source| {
                CodeCommandError::command(
                    "compare CodeWiki metadata",
                    Some(options.project_root.clone()),
                    source,
                )
            },
        )?;
        let text = compare_summary_text(&summary);
        return code_outcome(compare_scope, summary, text, None).map_err(|source| {
            CodeCommandError::command("serialize CodeWiki output", None, source)
        });
    }
    if options.complete_scope && options.scope.is_empty() {
        return Err(CodeCommandError::command(
            "generate CodeWiki output",
            Some(options.project_root.clone()),
            anyhow::anyhow!("--complete-scope requires at least one --scope path"),
        ));
    }
    let facts = CodewikiFacts::open(&options.project_root).map_err(|source| {
        CodeCommandError::command(
            "open CodeWiki facts",
            Some(options.project_root.clone()),
            source,
        )
    })?;
    let project_id = facts.project_id().to_string();
    let freshness = check_freshness(
        options.requires_freshness(),
        &options.project_root,
        options.allow_stale,
        ensure_project_fresh,
    )
    .map_err(CodeCommandError::Freshness)?;
    let busy_warning = matches!(freshness, Some(FreshnessStatus::SkippedBusy)).then_some(
        "warning: gcode index refresh already running; reading existing index".to_string(),
    );
    let mut source = crate::support::config::hub_ai_config_source("gwiki code")?;
    let ai_context = AiContext::resolve(Some(project_id.clone()), &mut source);
    let profiles = direct_profiles(&ai);
    let direct_targets = profiles.into_iter().map(|profile| {
        let target = resolve_direct_generation_target(&mut source, &profile);
        (profile, target)
    });
    let runtime = CodeEngineRuntime::new(
        options.project_root.clone(),
        project_id.clone(),
        options.quiet,
        options.verbose,
        ai_context,
        facts,
    )
    .with_direct_targets(direct_targets);

    if options.purge {
        let confirmation_out = options.out.clone();
        let Some(summary) =
            purge_summary(&runtime, options.out, options.force).map_err(|source| {
                CodeCommandError::command(
                    "purge CodeWiki output",
                    Some(options.project_root.clone()),
                    source,
                )
            })?
        else {
            let text = purge_confirmation(&runtime, confirmation_out.as_deref());
            return code_outcome(
                ScopeIdentity::project(project_id),
                serde_json::json!({"command": "codewiki purge", "confirmed": false}),
                text,
                busy_warning,
            )
            .map_err(|source| {
                CodeCommandError::command("serialize CodeWiki output", None, source)
            });
        };
        let text = purge_summary_text(&summary);
        return code_outcome(
            ScopeIdentity::project(project_id),
            summary,
            text,
            busy_warning,
        )
        .map_err(|source| CodeCommandError::command("serialize CodeWiki output", None, source));
    }
    if options.repair_citations {
        let summary = repair_summary(&runtime, options.out).map_err(|source| {
            CodeCommandError::command(
                "repair CodeWiki citations",
                Some(options.project_root.clone()),
                source,
            )
        })?;
        let text = repair_summary_text(&summary);
        return code_outcome(
            ScopeIdentity::project(project_id),
            summary,
            text,
            busy_warning,
        )
        .map_err(|source| CodeCommandError::command("serialize CodeWiki output", None, source));
    }
    let unscoped = options.complete_scope || options.scope.is_empty();
    let summary = run_summary(
        &runtime,
        options.out,
        options.scope,
        options.complete_scope,
        ai,
        options.edge_limit,
        options.include_docs,
        options.since,
        options.max_workers,
        options.verbose,
    )
    .map_err(|source| {
        CodeCommandError::command(
            "generate CodeWiki output",
            Some(options.project_root.clone()),
            source,
        )
    })?;
    let text = run_summary_text(&summary, unscoped);
    code_outcome(
        ScopeIdentity::project(project_id),
        summary,
        text,
        busy_warning,
    )
    .map_err(|source| CodeCommandError::command("serialize CodeWiki output", None, source))
}

fn code_outcome(
    scope: ScopeIdentity,
    summary: impl serde::Serialize,
    text: String,
    warning: Option<String>,
) -> anyhow::Result<CommandOutcome> {
    let mut outcome =
        crate::commands::scoped_outcome("code", &scope, serde_json::to_value(summary)?, text);
    if let Some(warning) = warning {
        outcome.status_messages.push(warning);
    }
    Ok(outcome)
}

fn check_freshness(
    required: bool,
    project_root: &std::path::Path,
    disabled: bool,
    check: impl FnOnce(&std::path::Path, bool) -> anyhow::Result<FreshnessStatus>,
) -> anyhow::Result<Option<FreshnessStatus>> {
    required.then(|| check(project_root, disabled)).transpose()
}

fn direct_profiles(ai: &CodewikiAiOptions) -> BTreeSet<String> {
    let mut profiles = [
        profile_for_tier(GenerationTier::Aggregate, None),
        profile_for_tier(GenerationTier::Module, None),
        profile_for_tier(GenerationTier::Standard, None),
    ]
    .into_iter()
    .collect::<BTreeSet<_>>();
    profiles.extend(ai.aggregate_profile.iter().cloned());
    profiles.extend(ai.verify_profile.iter().cloned());
    profiles
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;
    use std::path::Path;

    use super::*;

    #[test]
    fn code_error_boundary_preserves_error_categories_and_codes() {
        let input = WikiError::from(CodeCommandError::from(WikiError::InvalidInput {
            field: "ai-register",
            message: "unsupported register".to_string(),
        }));
        assert_eq!(input.code(), "invalid_input");

        let freshness = WikiError::from(CodeCommandError::Freshness(anyhow::anyhow!(
            "index is stale"
        )));
        assert_eq!(freshness.code(), "freshness_error");

        let io = WikiError::from(CodeCommandError::command(
            "read CodeWiki metadata",
            Some(PathBuf::from("wiki/_meta/codewiki.json")),
            std::io::Error::new(std::io::ErrorKind::PermissionDenied, "denied").into(),
        ));
        assert_eq!(io.code(), "io_error");

        let generation = WikiError::from(CodeCommandError::command(
            "generate CodeWiki output",
            None,
            anyhow::anyhow!("generation failed"),
        ));
        assert_eq!(generation.code(), "generation_error");
    }

    #[test]
    fn non_generation_modes_never_invoke_freshness() {
        let calls = Cell::new(0);
        for _ in 0..3 {
            let status = check_freshness(false, Path::new("/repo"), false, |_, _| {
                calls.set(calls.get() + 1);
                Ok(FreshnessStatus::Checked)
            })
            .expect("bypassed freshness");
            assert_eq!(status, None);
        }
        assert_eq!(calls.get(), 0);
    }

    #[test]
    fn generation_invokes_freshness_and_forwards_the_bypass_flag() {
        let status = check_freshness(true, Path::new("/repo"), true, |root, disabled| {
            assert_eq!(root, Path::new("/repo"));
            assert!(disabled);
            Ok(FreshnessStatus::SkippedBusy)
        })
        .expect("generation freshness");
        assert_eq!(status, Some(FreshnessStatus::SkippedBusy));
    }
}
