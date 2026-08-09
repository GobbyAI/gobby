use std::collections::BTreeSet;
use std::path::PathBuf;

use gobby_code::codewiki_facts::{CodewikiFacts, FreshnessStatus, ensure_project_fresh};
use gobby_core::ai::generation::{
    GenerationTier, profile_for_tier, resolve_direct_generation_target,
};
use gobby_core::ai_context::AiContext;

use crate::output::Format;
use crate::{CommandOutcome, ScopeIdentity};

use super::compare::compare_to;
use super::purge::{purge_confirmation, purge_summary, purge_summary_text};
use super::run::{repair_summary, repair_summary_text, run_summary, run_summary_text};
use super::{CodeEngineRuntime, CodewikiAiOptions};

pub const DEFAULT_CODE_GRAPH_EDGE_LIMIT: usize = 5_000;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CodeCommandOptions {
    pub project_root: PathBuf,
    pub out: Option<String>,
    pub purge: bool,
    pub force: bool,
    pub scope: Vec<String>,
    pub complete_scope: bool,
    pub ai: CodewikiAiOptions,
    pub edge_limit: usize,
    pub include_docs: bool,
    pub since: Option<String>,
    pub compare_to: Option<String>,
    pub max_workers: usize,
    pub repair_citations: bool,
    pub no_freshness: bool,
    pub format: Format,
    pub quiet: bool,
    pub verbose: bool,
}

impl CodeCommandOptions {
    pub fn requires_freshness(&self) -> bool {
        self.compare_to.is_none() && !self.purge && !self.repair_citations
    }
}

pub fn run_command(options: CodeCommandOptions) -> anyhow::Result<CommandOutcome> {
    if let Some(base_ref) = options.compare_to.as_deref() {
        let summary = compare_to(&options.project_root, options.out.as_deref(), base_ref)?;
        let text = serde_json::to_string_pretty(&summary)?;
        return code_outcome(
            ScopeIdentity::project(options.project_root.display().to_string()),
            summary,
            text,
            None,
        );
    }
    let freshness = check_freshness(
        options.requires_freshness(),
        &options.project_root,
        options.no_freshness,
        ensure_project_fresh,
    )?;
    let busy_warning = matches!(freshness, Some(FreshnessStatus::SkippedBusy)).then_some(
        "warning: gcode index refresh already running; reading existing index".to_string(),
    );
    let facts = CodewikiFacts::open(&options.project_root)?;
    let project_id = facts.project_id().to_string();
    let mut source = crate::support::config::hub_ai_config_source("gwiki code")?;
    let ai_context = AiContext::resolve(Some(project_id.clone()), &mut source);
    let profiles = direct_profiles(&options.ai);
    let direct_targets = profiles.into_iter().map(|profile| {
        let target = resolve_direct_generation_target(&mut source, &profile);
        (profile, target)
    });
    let runtime = CodeEngineRuntime::new(
        options.project_root.clone(),
        project_id.clone(),
        options.quiet,
        options.verbose,
        options.format,
        ai_context,
        facts,
    )
    .with_direct_targets(direct_targets);

    if options.purge {
        let confirmation_out = options.out.clone();
        let Some(summary) = purge_summary(&runtime, options.out, options.force)? else {
            let text = purge_confirmation(&runtime, confirmation_out.as_deref());
            return code_outcome(
                ScopeIdentity::project(project_id),
                serde_json::json!({"command": "codewiki purge", "confirmed": false}),
                text,
                busy_warning,
            );
        };
        let text = purge_summary_text(&summary);
        return code_outcome(
            ScopeIdentity::project(project_id),
            summary,
            text,
            busy_warning,
        );
    }
    if options.repair_citations {
        let summary = repair_summary(&runtime, options.out)?;
        let text = repair_summary_text(&summary);
        return code_outcome(
            ScopeIdentity::project(project_id),
            summary,
            text,
            busy_warning,
        );
    }
    let unscoped = options.complete_scope || options.scope.is_empty();
    let summary = run_summary(
        &runtime,
        options.out,
        options.scope,
        options.complete_scope,
        options.ai,
        options.edge_limit,
        options.include_docs,
        options.since,
        options.max_workers,
        options.verbose,
    )?;
    let text = run_summary_text(&summary, unscoped);
    code_outcome(
        ScopeIdentity::project(project_id),
        summary,
        text,
        busy_warning,
    )
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
