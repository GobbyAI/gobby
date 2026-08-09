use std::collections::BTreeSet;
use std::path::PathBuf;

use gobby_code::codewiki_facts::CodewikiFacts;
use gobby_core::ai::generation::{
    GenerationTier, profile_for_tier, resolve_direct_generation_target,
};
use gobby_core::ai_context::AiContext;

use crate::output::Format;

use super::{CodeEngineRuntime, CodewikiAiOptions, run, run_compare, run_purge, run_repair};

pub const DEFAULT_CODE_GRAPH_EDGE_LIMIT: usize = 5_000;

#[derive(Clone, Debug)]
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
    pub format: Format,
    pub quiet: bool,
    pub verbose: bool,
}

pub fn run_command(options: CodeCommandOptions) -> anyhow::Result<()> {
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
        options.project_root,
        project_id,
        options.quiet,
        options.verbose,
        options.format,
        ai_context,
        facts,
    )
    .with_direct_targets(direct_targets);

    if let Some(base_ref) = options.compare_to {
        return run_compare(&runtime, options.out, &base_ref);
    }
    if options.purge {
        return run_purge(&runtime, options.out, options.force, options.format);
    }
    if options.repair_citations {
        return run_repair(&runtime, options.out, options.format);
    }
    run(
        &runtime,
        options.out,
        options.scope,
        options.complete_scope,
        options.ai,
        options.edge_limit,
        options.include_docs,
        options.since,
        options.max_workers,
        options.format,
        options.verbose,
    )
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
