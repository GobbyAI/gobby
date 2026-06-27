//! Standalone (Direct-route) resolution of a named text-generation feature
//! profile into a concrete chat-completion target.
//!
//! In daemon mode the profile name is forwarded to the daemon, which owns
//! provider/model selection. In standalone mode there is no daemon, so a
//! profile such as `feature_high` must resolve to a concrete
//! provider/model/api_base/api_key here, read from `~/.gobby/gcore.yaml`.
//!
//! Resolution reads `ai.text_generate.profiles.<profile>.<field>` and falls
//! back to the base `ai.text_generate.<field>` binding when a profile-specific
//! field is unset. All values flow through [`resolve_ai_setting`], so
//! `$secret:` and `${ENV}` patterns resolve exactly as they do for the base
//! binding, and a plaintext `api_key` in standalone YAML is accepted.

use crate::config::{ConfigSource, ai_keys, resolve_ai_setting};

/// A resolved Direct-route chat-completion target for a feature profile.
///
/// Every field is optional because a standalone config may rely on server
/// defaults (e.g. an LM Studio endpoint that ignores the model field). The
/// generation lanes treat a missing `api_base` as "not configured for Direct".
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DirectGenerationTarget {
    /// OpenAI-compatible API base (e.g. `http://127.0.0.1:1234/v1`).
    pub api_base: Option<String>,
    /// Bearer API key. Plaintext is permitted in standalone YAML.
    pub api_key: Option<String>,
    /// Model identifier sent in the chat-completions body.
    pub model: Option<String>,
    /// Informational provider label (not pinned to any vendor).
    pub provider: Option<String>,
    /// Optional reasoning-effort hint forwarded to capable local servers.
    pub reasoning_effort: Option<String>,
}

impl DirectGenerationTarget {
    /// The trimmed, non-empty API base if one is configured.
    pub fn api_base(&self) -> Option<&str> {
        non_empty(self.api_base.as_deref())
    }
}

/// Resolve the Direct-route target for `profile` from a config source.
///
/// Each field prefers `ai.text_generate.profiles.<profile>.<field>` and falls
/// back to the base `ai.text_generate.<field>` value. The base fallback keeps a
/// single-endpoint standalone config working without per-profile blocks: all
/// tiers then resolve to the same target.
pub fn resolve_direct_generation_target(
    source: &mut impl ConfigSource,
    profile: &str,
) -> DirectGenerationTarget {
    DirectGenerationTarget {
        api_base: profile_or_base(source, profile, ai_keys::PROFILE_API_BASE),
        api_key: profile_or_base(source, profile, ai_keys::PROFILE_API_KEY),
        model: profile_or_base(source, profile, ai_keys::PROFILE_MODEL),
        provider: profile_or_base(source, profile, ai_keys::PROFILE_PROVIDER),
        reasoning_effort: profile_reasoning_effort(source, profile),
    }
}

/// Profile-specific field, falling back to the base `ai.text_generate.<field>`.
fn profile_or_base(source: &mut impl ConfigSource, profile: &str, suffix: &str) -> Option<String> {
    let profile_key = ai_keys::text_generate_profile_key(profile, suffix);
    resolve_ai_setting(source, &profile_key)
        .or_else(|| resolve_ai_setting(source, base_key(suffix)))
}

/// Reasoning effort has no base key under the standard suffix convention; it
/// uses the dedicated `ai.text_generate.reasoning_effort` base key.
fn profile_reasoning_effort(source: &mut impl ConfigSource, profile: &str) -> Option<String> {
    let profile_key =
        ai_keys::text_generate_profile_key(profile, ai_keys::PROFILE_REASONING_EFFORT);
    resolve_ai_setting(source, &profile_key)
        .or_else(|| resolve_ai_setting(source, ai_keys::TEXT_GENERATE_REASONING_EFFORT))
}

/// Map a `PROFILE_*` suffix to its base `ai.text_generate.<field>` key.
fn base_key(suffix: &str) -> &'static str {
    match suffix {
        s if s == ai_keys::PROFILE_API_BASE => ai_keys::TEXT_GENERATE_API_BASE,
        s if s == ai_keys::PROFILE_API_KEY => ai_keys::TEXT_GENERATE_API_KEY,
        s if s == ai_keys::PROFILE_MODEL => ai_keys::TEXT_GENERATE_MODEL,
        s if s == ai_keys::PROFILE_PROVIDER => ai_keys::TEXT_GENERATE_PROVIDER,
        _ => ai_keys::TEXT_GENERATE_REASONING_EFFORT,
    }
}

fn non_empty(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|value| !value.is_empty())
}
