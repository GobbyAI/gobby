use gobby_core::ai::generation::{DirectGenerationTarget, GenerationTier, profile_for_tier};
use gobby_core::ai_context::AiContext;
use gobby_core::config::{AiRouting, FeatureCandidate};

use crate::commands::code::{CodeEngineRuntime, PromptTier};

/// Run-level guard for `--ai-aggregate-candidate` on the Direct route: an
/// explicit candidate chain can only be honored by the daemon (the Direct
/// route resolves a single profile target), so a direct one-shot or an
/// engaged direct tool loop must fail the whole run loudly instead of
/// degrading every aggregate page. Returns the error message to surface, or
/// `None` when the pinned run may proceed.
pub(crate) fn direct_route_candidate_error(
    aggregate_candidates: &[FeatureCandidate],
    text_route: AiRouting,
    engaged_tool_loop_route: Option<AiRouting>,
) -> Option<String> {
    if aggregate_candidates.is_empty() {
        return None;
    }
    let direct =
        text_route == AiRouting::Daemon || engaged_tool_loop_route == Some(AiRouting::Daemon);
    direct.then(|| {
        "--ai-aggregate-candidate requires the daemon route (explicit candidates are \
         unsupported on the Direct route); rerun with --ai daemon, or drop the flag and \
         use --ai-aggregate-profile"
            .to_string()
    })
}

/// Maps the codewiki prompt tier onto the shared, provider-neutral generation
/// tier owned by gcore. The three tiers line up one-to-one.
pub(super) fn generation_tier(tier: PromptTier) -> GenerationTier {
    match tier {
        PromptTier::Aggregate => GenerationTier::Aggregate,
        PromptTier::Module => GenerationTier::Module,
        PromptTier::Standard => GenerationTier::Standard,
    }
}

/// Direct-route generation targets resolved once per tier, so a standalone
/// `gcore.yaml` can route `feature_low/mid/high` to their own
/// provider/model/api_key. Only built when the resolved route is Direct.
pub(super) struct DirectTierTargets {
    aggregate: DirectGenerationTarget,
    module: DirectGenerationTarget,
    standard: DirectGenerationTarget,
}

impl DirectTierTargets {
    pub(super) fn for_tier(&self, tier: GenerationTier) -> &DirectGenerationTarget {
        match tier {
            GenerationTier::Aggregate => &self.aggregate,
            GenerationTier::Module => &self.module,
            GenerationTier::Standard => &self.standard,
        }
    }

    pub(super) fn all_tiers_usable(&self) -> bool {
        self.aggregate.api_base().is_some()
            && self.module.api_base().is_some()
            && self.standard.api_base().is_some()
    }
}

pub(super) fn resolve_direct_targets<const N: usize>(
    ctx: &CodeEngineRuntime,
    profiles: [String; N],
) -> [DirectGenerationTarget; N] {
    std::array::from_fn(|index| ctx.direct_target(&profiles[index]))
}

/// Resolve a Direct-route target per tier from the AI config source.
pub(super) fn resolve_direct_tier_targets(
    ctx: &CodeEngineRuntime,
    aggregate_override: Option<&str>,
) -> DirectTierTargets {
    let [aggregate, module, standard] = resolve_direct_targets(
        ctx,
        [
            profile_for_tier(GenerationTier::Aggregate, aggregate_override),
            profile_for_tier(GenerationTier::Module, None),
            profile_for_tier(GenerationTier::Standard, None),
        ],
    );
    DirectTierTargets {
        aggregate,
        module,
        standard,
    }
}

pub(super) fn resolve_ai_context(ctx: &CodeEngineRuntime, ai: Option<AiRouting>) -> AiContext {
    let mut context = ctx.ai.clone();
    context.project_id = Some(ctx.project_id.clone());
    if let Some(routing) = ai {
        context.bindings.text_generate.routing = routing;
    }
    context
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::ai::generation::{FEATURE_HIGH, FEATURE_LOW, FEATURE_MID};

    #[test]
    fn prompt_tier_maps_to_feature_profiles() {
        assert_eq!(
            generation_tier(PromptTier::Aggregate),
            GenerationTier::Aggregate
        );
        assert_eq!(generation_tier(PromptTier::Module), GenerationTier::Module);
        assert_eq!(
            generation_tier(PromptTier::Standard),
            GenerationTier::Standard
        );

        assert_eq!(
            profile_for_tier(generation_tier(PromptTier::Aggregate), None),
            FEATURE_HIGH
        );
        assert_eq!(
            profile_for_tier(
                generation_tier(PromptTier::Aggregate),
                Some("custom-writer")
            ),
            "custom-writer"
        );
        assert_eq!(
            profile_for_tier(generation_tier(PromptTier::Module), None),
            FEATURE_MID
        );
        assert_eq!(
            profile_for_tier(generation_tier(PromptTier::Standard), None),
            FEATURE_LOW
        );
    }

    #[test]
    fn direct_tier_targets_require_api_base_for_every_tier() {
        fn target(api_base: Option<&str>) -> DirectGenerationTarget {
            DirectGenerationTarget {
                api_base: api_base.map(str::to_string),
                ..DirectGenerationTarget::default()
            }
        }

        assert!(
            DirectTierTargets {
                aggregate: target(Some("http://aggregate.test/v1")),
                module: target(Some("http://module.test/v1")),
                standard: target(Some("http://standard.test/v1")),
            }
            .all_tiers_usable()
        );

        for targets in [
            DirectTierTargets {
                aggregate: target(None),
                module: target(Some("http://module.test/v1")),
                standard: target(Some("http://standard.test/v1")),
            },
            DirectTierTargets {
                aggregate: target(Some("http://aggregate.test/v1")),
                module: target(None),
                standard: target(Some("http://standard.test/v1")),
            },
            DirectTierTargets {
                aggregate: target(Some("http://aggregate.test/v1")),
                module: target(Some("http://module.test/v1")),
                standard: target(None),
            },
        ] {
            assert!(!targets.all_tiers_usable());
        }
    }
}
