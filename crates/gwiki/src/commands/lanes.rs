//! Shared AI lane resolution for article-synthesizing commands (`compile`,
//! `upkeep`): Lane B tool-loop generators and the Lane A one-shot explainer
//! transport, resolved through the same gcore routing every other gwiki
//! capability uses.

use std::path::{Path, PathBuf};

use gobby_core::ai::generation::{
    ChatMessage, ChatTransport, DirectChatTransport, DirectGenerationTarget, GenerationTier,
    ToolLoopLimits, ToolPolicy, daemon_agentic_chat, generate_one_shot, profile_for_tier,
    resolve_direct_generation_target, run_tool_loop,
};
use gobby_core::ai::{AiNoticeKind, resolve_route_observed};
use gobby_core::ai_context::{AiContext, AiContextOptions};
use gobby_core::config::{AiCapability, AiRouting};

use crate::explainer::{ExplainerPrompt, ExplainerResponse};
use crate::{ScopeIdentity, ScopeSelection};

use super::vault_tools::VaultToolExecutor;

/// Compiled wiki articles are gwiki's curated narrative surface, so they
/// generate on the aggregate tier — for both the `compile` command and the
/// `upkeep` conductor, which synthesize through the same pipeline. Tier ->
/// feature profile is owned by gcore's `profile_for_tier` (Aggregate ->
/// feature_high); provider/model resolution stays in config and is never
/// pinned here. The Daemon route forwards the resolved profile name; the
/// Direct route resolves it to a concrete target so a standalone gcore.yaml
/// routes synthesis to its own provider/model/api_key.
pub(crate) const ARTICLE_TIER: GenerationTier = GenerationTier::Aggregate;

/// Owned Lane B explainer generator (the boxed counterpart of the borrowed
/// [`crate::explainer::ExplainerGenerator`]).
pub(crate) type BoxedExplainerGenerator =
    Box<dyn FnMut(&ExplainerPrompt) -> Result<ExplainerResponse, String>>;

/// A resolved Lane B generator plus the routing metadata to report.
pub(crate) struct LaneB {
    pub(crate) generator: BoxedExplainerGenerator,
    pub(crate) info: LaneBInfo,
}

/// Read-only gwiki subcommands the daemon's agent may run during a Lane B
/// investigation. Must stay a subset of the daemon's `GWIKI_READONLY_TOOLS`
/// whitelist in `src/gobby/ai/_tool_chat_tools.py` — the daemon rejects the
/// whole policy if any listed subcommand is off its allowlist.
const GWIKI_READONLY_TOOLS: [&str; 8] = [
    "search",
    "read",
    "backlinks",
    "sources",
    "status",
    "trust",
    "audit",
    "lint",
];

/// Turn budget for the daemon's server-side agent, matching the client-side
/// [`ToolLoopLimits::default`] bound the Direct route runs under.
const DAEMON_AGENTIC_MAX_TURNS: usize = 8;

/// The read-only gwiki investigation policy Lane B hands the daemon: the agent
/// may inspect the vault but never mutate it.
fn gwiki_readonly_tool_policy() -> ToolPolicy {
    ToolPolicy {
        cli: "gwiki".to_string(),
        tools: GWIKI_READONLY_TOOLS
            .iter()
            .map(|tool| (*tool).to_string())
            .collect(),
        allow_mutation: false,
    }
}

#[derive(Clone, Copy)]
pub(crate) struct LaneBInfo {
    pub(crate) route_label: &'static str,
    pub(crate) fallback: bool,
    pub(crate) notice: Option<AiNoticeKind>,
}

/// Resolve a Lane B tool-loop generator, mirroring codewiki's
/// `resolve_tool_loop_generator` (#978). Returns `None` (so the caller falls back
/// to the Lane A one-shot explainer) when AI is off, no tool-chat route resolves,
/// a Direct route lacks a usable `api_base`, or a Daemon route lacks a project
/// root (the daemon's agent investigates with `cwd=project_path`, which topic
/// scopes cannot supply).
pub(crate) fn resolve_lane_b_generator(
    requested: AiRouting,
    scope: &ScopeSelection,
    vault_root: PathBuf,
    project_root: Option<PathBuf>,
    scope_identity: ScopeIdentity,
    command: &'static str,
) -> Option<LaneB> {
    if matches!(requested, AiRouting::Off) {
        return None;
    }
    let mut source = crate::support::config::hub_ai_config_source(command).ok()?;
    let context = AiContext::resolve_with_options(
        None,
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: Some(requested),
        },
    );
    let observed = resolve_route_observed(&context, AiCapability::ToolChat);
    let route = observed.route;
    if matches!(route, AiRouting::Off | AiRouting::Auto) {
        return None;
    }
    // A Daemon-route Lane B without a project root (topic scope) cannot give
    // the daemon's agent an investigation cwd; decline so the caller falls
    // back to the Lane A one-shot explainer, which needs no project root.
    if matches!(route, AiRouting::Daemon) && project_root.is_none() {
        return None;
    }
    let profile = profile_for_tier(ARTICLE_TIER, None);
    let target = if matches!(route, AiRouting::Direct) {
        let target =
            resolve_direct_generation_target(&mut source, &profile_for_tier(ARTICLE_TIER, None));
        // A Direct-route Lane B with no resolved api_base cannot run; decline so
        // the caller falls back to the Lane A one-shot explainer.
        target.api_base()?;
        Some(target)
    } else {
        None
    };
    let info = LaneBInfo {
        route_label: routing_label(route),
        fallback: observed.fallback,
        notice: observed.reason.or_else(|| {
            (observed.fallback && route == AiRouting::Direct)
                .then_some(AiNoticeKind::AutoFallbackToDirect)
        }),
    };
    let scope = scope.clone();
    let generator: BoxedExplainerGenerator = Box::new(move |prompt: &ExplainerPrompt| {
        run_lane_b(
            &context,
            route,
            &profile,
            target.as_ref(),
            project_root.as_deref(),
            prompt,
            &scope,
            &vault_root,
            &scope_identity,
        )
    });
    Some(LaneB { generator, info })
}

/// Run one Lane B generation. Hard-fails (returns `Err`) on generation failure
/// or empty content — callers fail the article instead of writing a skeleton.
///
/// Daemon route: one server-side agentic POST via [`daemon_agentic_chat`]. The
/// daemon's agent investigates with whitelisted read-only gwiki subcommands in
/// `cwd=project_root` and returns the finished narrative — no local tool loop
/// runs (the agentic endpoint never returns `tool_calls`; a passthrough
/// transport would re-prompt forever, and did 422 before this port because it
/// omitted the route's required `project_path`/`tool_policy` fields).
///
/// Direct route: the local tool loop where the model investigates the vault
/// via [`VaultToolExecutor`]. Data-source degradation mid-loop is logged as
/// evidence, never a generation failure.
#[allow(clippy::too_many_arguments)]
fn run_lane_b(
    context: &AiContext,
    route: AiRouting,
    profile: &str,
    target: Option<&DirectGenerationTarget>,
    project_root: Option<&Path>,
    prompt: &ExplainerPrompt,
    scope: &ScopeSelection,
    vault_root: &Path,
    scope_identity: &ScopeIdentity,
) -> Result<ExplainerResponse, String> {
    let messages = vec![
        ChatMessage::system(prompt.system.to_string()),
        ChatMessage::user(prompt.user.clone()),
    ];
    match route {
        AiRouting::Daemon => {
            let project_root = project_root
                .ok_or_else(|| "daemon Lane B requires a project scope root".to_string())?;
            let binding = context.binding(AiCapability::ToolChat);
            let result = daemon_agentic_chat(
                context,
                profile,
                None,
                &project_root.display().to_string(),
                &gwiki_readonly_tool_policy(),
                &messages,
                Some(DAEMON_AGENTIC_MAX_TURNS),
                binding.reasoning_effort.as_deref(),
            )
            .map_err(|error| error.to_string())?;
            let text = result
                .content
                .filter(|text| !text.trim().is_empty())
                .ok_or_else(|| "daemon agent returned no content".to_string())?;
            Ok(ExplainerResponse {
                text,
                model: result.model,
                route: routing_label(route),
            })
        }
        AiRouting::Direct => {
            let target = target
                .ok_or_else(|| "direct Lane B requires a resolved profile target".to_string())?;
            let transport =
                DirectChatTransport::new(context, target.clone(), Some(profile.to_string()))
                    .map_err(|error| error.to_string())?;
            let model = transport.model().map(str::to_string);
            let mut executor = VaultToolExecutor::new(
                scope.clone(),
                vault_root.to_path_buf(),
                scope_identity.clone(),
            );
            let limits = ToolLoopLimits::default();
            let outcome = run_tool_loop(&transport, &mut executor, messages, &limits, None)
                .map_err(|error| error.to_string())?;
            // Data-source degradation (graph/semantic backend down mid-loop) is
            // evidence degradation, not a generation failure — log it without
            // hard-failing.
            let degraded = executor.into_data_source_degraded();
            if !degraded.is_empty() {
                log::warn!(
                    "Lane B: data-source degradation during tool loop: {}",
                    degraded.join(", ")
                );
            }
            if !outcome.stop_reason.is_completed() {
                return Err(format!(
                    "tool loop did not complete ({})",
                    outcome.stop_reason.as_str()
                ));
            }
            let content = outcome
                .content
                .filter(|text| !text.trim().is_empty())
                .ok_or_else(|| "tool loop returned no content".to_string())?;
            Ok(ExplainerResponse {
                text: content,
                model,
                route: routing_label(route),
            })
        }
        AiRouting::Off | AiRouting::Auto => Err("tool-chat route is off or unresolved".to_string()),
    }
}

/// Resolved explainer transport, mirroring `gwiki ask` honesty semantics:
/// `Off` skips synthesis structurally; an unresolved explicit daemon/direct
/// request still runs an attempt so the failure is recorded as degradation.
pub(crate) enum ExplainerTransport {
    Off {
        fallback: bool,
        notice: Option<AiNoticeKind>,
    },
    Unresolved {
        route: AiRouting,
        fallback: bool,
        notice: Option<AiNoticeKind>,
        error: String,
    },
    Resolved {
        route: AiRouting,
        fallback: bool,
        notice: Option<AiNoticeKind>,
        context: Box<AiContext>,
        /// Per-tier direct-generation target resolved for [`ARTICLE_TIER`],
        /// present only on the Direct route; the Daemon route forwards the
        /// profile name and leaves this `None`.
        target: Option<DirectGenerationTarget>,
    },
}

impl ExplainerTransport {
    pub(crate) fn off(fallback: bool, notice: Option<AiNoticeKind>) -> Self {
        Self::Off { fallback, notice }
    }

    pub(crate) fn is_active(&self) -> bool {
        !matches!(self, Self::Off { .. })
    }

    pub(crate) fn route_label(&self) -> &'static str {
        match self {
            Self::Off { .. } => "off",
            Self::Unresolved { route, .. } | Self::Resolved { route, .. } => routing_label(*route),
        }
    }

    pub(crate) fn fallback(&self) -> bool {
        match self {
            Self::Off { fallback, .. } => *fallback,
            Self::Unresolved { fallback, .. } | Self::Resolved { fallback, .. } => *fallback,
        }
    }

    pub(crate) fn notice_kind(&self) -> Option<AiNoticeKind> {
        match self {
            Self::Off { notice, .. } => *notice,
            Self::Unresolved { notice, .. } | Self::Resolved { notice, .. } => *notice,
        }
    }

    pub(crate) fn generate(&self, prompt: &ExplainerPrompt) -> Result<ExplainerResponse, String> {
        match self {
            Self::Off { .. } => Err("AI synthesis is off".to_string()),
            Self::Unresolved { error, .. } => Err(error.clone()),
            Self::Resolved {
                route,
                context,
                target,
                ..
            } => {
                let result = generate_one_shot(
                    context,
                    *route,
                    ARTICLE_TIER,
                    None,
                    target.as_ref(),
                    &prompt.user,
                    Some(prompt.system),
                    None,
                )
                .map_err(|error| error.to_string())?;
                Ok(ExplainerResponse {
                    text: result.text,
                    model: result.model,
                    route: routing_label(*route),
                })
            }
        }
    }
}

/// Resolve the AI route for explainer synthesis through the same gcore
/// routing every other gwiki capability uses. `auto` that resolves to no
/// usable route degrades to a structural skip rather than a failure.
pub(crate) fn resolve_explainer_transport(
    requested: AiRouting,
    command: &'static str,
) -> ExplainerTransport {
    if matches!(requested, AiRouting::Off) {
        return ExplainerTransport::off(false, None);
    }
    match crate::support::config::hub_ai_config_source(command) {
        Ok(mut source) => {
            let context = AiContext::resolve_with_options(
                None,
                &mut source,
                AiContextOptions {
                    no_ai: false,
                    forced_routing: Some(requested),
                },
            );
            let observed = resolve_route_observed(&context, AiCapability::TextGenerate);
            match observed.route {
                route @ (AiRouting::Daemon | AiRouting::Direct) => {
                    // The Direct route needs a concrete per-tier target resolved
                    // from the same config source (hub config_store plus any
                    // standalone gcore.yaml). The Daemon route forwards the
                    // profile name and ignores the target.
                    let target = matches!(route, AiRouting::Direct).then(|| {
                        resolve_direct_generation_target(
                            &mut source,
                            &profile_for_tier(ARTICLE_TIER, None),
                        )
                    });
                    if target
                        .as_ref()
                        .is_some_and(|target| target.api_base().is_none())
                    {
                        return ExplainerTransport::Unresolved {
                            route,
                            fallback: observed.fallback,
                            notice: Some(AiNoticeKind::NoGenerator),
                            error: "direct AI synthesis requires ai.text_generate api_base"
                                .to_string(),
                        };
                    }
                    ExplainerTransport::Resolved {
                        route,
                        fallback: observed.fallback,
                        notice: observed.reason.or_else(|| {
                            (observed.fallback && route == AiRouting::Direct)
                                .then_some(AiNoticeKind::AutoFallbackToDirect)
                        }),
                        context: Box::new(context),
                        target,
                    }
                }
                _ => ExplainerTransport::off(observed.fallback, observed.reason),
            }
        }
        Err(error) => match requested {
            AiRouting::Daemon | AiRouting::Direct => ExplainerTransport::Unresolved {
                route: requested,
                fallback: false,
                notice: Some(AiNoticeKind::NoGenerator),
                error: error.to_string(),
            },
            AiRouting::Auto => ExplainerTransport::Unresolved {
                route: AiRouting::Off,
                fallback: true,
                notice: Some(AiNoticeKind::NoGenerator),
                error: error.to_string(),
            },
            _ => ExplainerTransport::off(false, None),
        },
    }
}

pub(crate) fn notice_for_explainer_status(
    status: &str,
    notice: Option<AiNoticeKind>,
) -> Option<AiNoticeKind> {
    match (status, notice) {
        ("failed", None) => Some(AiNoticeKind::GenerationFailed),
        (_, notice) => notice,
    }
}

pub(crate) fn routing_label(route: AiRouting) -> &'static str {
    match route {
        AiRouting::Auto => "auto",
        AiRouting::Daemon => "daemon",
        AiRouting::Direct => "direct",
        AiRouting::Off => "off",
    }
}

pub(crate) fn ai_notice_label(notice: AiNoticeKind) -> &'static str {
    match notice {
        AiNoticeKind::AutoFallbackToDirect => "auto_fallback_to_direct",
        AiNoticeKind::AutoFallbackToOff => "auto_fallback_to_off",
        AiNoticeKind::NoGenerator => "no_generator",
        AiNoticeKind::GenerationFailed => "generation_failed",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::ai::ObservedAiRoute;
    use gobby_core::ai_context::{AiBindings, AiLimiter};
    use gobby_core::config::{AiTuning, CapabilityBinding};

    #[test]
    fn articles_generate_on_the_aggregate_feature_profile() {
        use gobby_core::ai::generation::FEATURE_HIGH;
        assert_eq!(ARTICLE_TIER, GenerationTier::Aggregate);
        assert_eq!(profile_for_tier(ARTICLE_TIER, None), FEATURE_HIGH);
    }

    /// Pins the daemon Lane B investigation policy to the daemon's
    /// `GWIKI_READONLY_TOOLS` whitelist (`src/gobby/ai/_tool_chat_tools.py`).
    /// Adding a subcommand here without updating the daemon allowlist makes the
    /// daemon reject the whole policy, so this list must only change in
    /// lockstep with the daemon side.
    #[test]
    fn daemon_lane_b_policy_is_readonly_gwiki_whitelist() {
        let policy = gwiki_readonly_tool_policy();
        assert_eq!(policy.cli, "gwiki");
        assert!(!policy.allow_mutation);
        assert_eq!(
            policy.tools,
            vec![
                "search",
                "read",
                "backlinks",
                "sources",
                "status",
                "trust",
                "audit",
                "lint",
            ]
        );
    }

    #[test]
    fn observed_auto_daemon_up_ignores_direct_config() {
        let context = ai_context(AiRouting::Auto, Some("http://direct.test"));

        assert_eq!(
            gobby_core::ai::resolve_route_observed_with_probe(
                &context,
                AiCapability::TextGenerate,
                |_| true,
            ),
            ObservedAiRoute {
                route: AiRouting::Daemon,
                fallback: false,
                reason: None,
            }
        );
    }

    #[test]
    fn observed_explicit_daemon_stays_daemon_when_probe_unavailable() {
        let context = ai_context(AiRouting::Daemon, Some("http://direct.test"));

        assert_eq!(
            gobby_core::ai::resolve_route_observed_with_probe(
                &context,
                AiCapability::TextGenerate,
                |_| false,
            ),
            ObservedAiRoute {
                route: AiRouting::Daemon,
                fallback: false,
                reason: None,
            }
        );
    }

    #[test]
    fn off_transport_preserves_fallback_notice_metadata() {
        let transport = ExplainerTransport::off(true, Some(AiNoticeKind::AutoFallbackToOff));

        assert!(!transport.is_active());
        assert_eq!(transport.route_label(), "off");
        assert!(transport.fallback());
        assert_eq!(
            transport.notice_kind(),
            Some(AiNoticeKind::AutoFallbackToOff)
        );
    }

    #[test]
    fn failed_explainer_status_preserves_existing_notice() {
        assert_eq!(
            notice_for_explainer_status("failed", Some(AiNoticeKind::NoGenerator)),
            Some(AiNoticeKind::NoGenerator)
        );
        assert_eq!(
            notice_for_explainer_status("failed", None),
            Some(AiNoticeKind::GenerationFailed)
        );
        assert_eq!(
            notice_for_explainer_status("generated", Some(AiNoticeKind::AutoFallbackToDirect)),
            Some(AiNoticeKind::AutoFallbackToDirect)
        );
    }

    fn ai_context(routing: AiRouting, api_base: Option<&str>) -> AiContext {
        let binding = CapabilityBinding {
            routing,
            transport: None,
            api_base: api_base.map(str::to_string),
            api_key: None,
            model: None,
            provider: None,
            task: None,
            language: None,
            target_lang: None,
            profile: None,
            candidates: None,
            reasoning_effort: None,
            verify_profile: None,
            verify_model: None,
            verify_api_key: None,
        };
        AiContext {
            bindings: AiBindings {
                embed: binding.clone(),
                audio_transcribe: binding.clone(),
                audio_translate: binding.clone(),
                vision_extract: binding.clone(),
                text_generate: binding,
            },
            tuning: AiTuning {
                max_concurrency: 1,
                keep_alive: None,
            },
            limiter: AiLimiter::new(1),
            project_id: None,
        }
    }
}
