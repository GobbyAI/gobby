//! Provider-neutral generation tiers and the single tier -> feature-profile
//! resolver shared by the Direct and Daemon routes.
//!
//! The mapping is the one authoritative place that turns a writing tier into a
//! daemon feature profile name:
//!
//! | Tier        | Profile         |
//! |-------------|-----------------|
//! | `Aggregate` | `feature_high`* |
//! | `Module`    | `feature_mid`   |
//! | `Standard`  | `feature_low`   |
//!
//! \* `Aggregate` honors an explicit override (e.g. `--ai-aggregate-profile`).
//!
//! The Daemon route forwards the resolved profile name verbatim; the Direct
//! route feeds it to [`super::profile::resolve_direct_generation_target`] to
//! look up provider/model/api_base/api_key from `~/.gobby/gcore.yaml`.

/// Lightest writing tier: per-file/per-symbol summaries.
pub const FEATURE_LOW: &str = "feature_low";
/// Mid tier: module-level prose.
pub const FEATURE_MID: &str = "feature_mid";
/// Highest tier: aggregate curated narrative (overview/architecture/etc.).
pub const FEATURE_HIGH: &str = "feature_high";

/// Provider-neutral writing tier. Mirrors the consumer-side prompt tier without
/// pulling any gcode/gwiki types into gcore.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum GenerationTier {
    /// Per-file / per-symbol summaries.
    #[default]
    Standard,
    /// Module-level prose.
    Module,
    /// Aggregate curated narrative.
    Aggregate,
}

impl GenerationTier {
    /// The default feature profile for this tier, ignoring any aggregate
    /// override. See [`profile_for_tier`] for the override-aware entry point.
    pub fn default_profile(self) -> &'static str {
        match self {
            GenerationTier::Standard => FEATURE_LOW,
            GenerationTier::Module => FEATURE_MID,
            GenerationTier::Aggregate => FEATURE_HIGH,
        }
    }
}

/// Resolve the feature profile name for a writing tier.
///
/// `aggregate_override` (e.g. from `--ai-aggregate-profile`) replaces the
/// default `feature_high` for the [`GenerationTier::Aggregate`] tier only; it is
/// ignored for the other tiers, which keep their fixed mapping. An empty/blank
/// override is treated as absent.
pub fn profile_for_tier(tier: GenerationTier, aggregate_override: Option<&str>) -> String {
    match tier {
        GenerationTier::Aggregate => aggregate_override
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or(FEATURE_HIGH)
            .to_string(),
        other => other.default_profile().to_string(),
    }
}
