use gobby_core::config::{AiRouting, FeatureCandidate};

use super::CodewikiDocMeta;

pub(crate) const REPO_DOC_PATH: &str = "code/repo.md";
pub(crate) const ARCHITECTURE_DOC_PATH: &str = "code/_architecture.md";
pub(crate) const CONCEPT_INDEX_DOC_PATH: &str = "code/concepts/index.md";
pub(crate) const CONCEPT_DOC_PREFIX: &str = "code/concepts/";
pub(crate) const NARRATIVE_DOC_PREFIX: &str = "code/narrative/";

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub(crate) enum AiGenerationStatus {
    Generated,
    Degraded,
    Skipped,
}

impl AiGenerationStatus {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Generated => "generated",
            Self::Degraded => "degraded",
            Self::Skipped => "skipped",
        }
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub(crate) struct CodewikiAiOutcome {
    pub(crate) route: AiRouting,
    pub(crate) fallback: bool,
    pub(crate) status: AiGenerationStatus,
}

impl CodewikiAiOutcome {
    pub(crate) fn skipped(route: AiRouting, fallback: bool) -> Self {
        Self {
            route,
            fallback,
            status: AiGenerationStatus::Skipped,
        }
    }

    pub(crate) fn generated(route: AiRouting, fallback: bool) -> Self {
        Self {
            route,
            fallback,
            status: AiGenerationStatus::Generated,
        }
    }

    pub(crate) fn for_doc(self, degraded: bool) -> Self {
        if degraded {
            Self {
                status: AiGenerationStatus::Degraded,
                ..self
            }
        } else {
            self
        }
    }

    pub(crate) fn route_label(self) -> &'static str {
        ai_route_label(self.route)
    }
}

impl Default for CodewikiAiOutcome {
    fn default() -> Self {
        Self::skipped(AiRouting::Off, false)
    }
}

fn ai_route_label(route: AiRouting) -> &'static str {
    match route {
        AiRouting::Daemon => "daemon",
        AiRouting::Off => "off",
    }
}

/// The requested AI generation settings of the current run, in the canonical
/// recorded form compared against [`CodewikiDocMeta`] (#17530). All fields are
/// empty for a default run, matching meta written before settings were
/// recorded, so default runs never self-invalidate.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct AiGenerationSettings {
    /// Requested `--ai-prose-depth` (`brief`/`deep`); empty for the default
    /// `standard`, which defers to the provider/profile budget.
    pub(crate) prose_depth: String,
    /// Requested `--ai-register` (`newcomer`/`maintainer`/`agent`); empty for
    /// the default base voice.
    pub(crate) register: String,
    /// Requested `--ai-aggregate-profile` override; empty when unset.
    pub(crate) aggregate_profile: String,
    /// Requested `--ai-aggregate-candidate` chain as ordered canonical
    /// `provider/model[@effort]` labels; empty when unset.
    pub(crate) aggregate_candidates: Vec<String>,
}

impl AiGenerationSettings {
    pub(crate) fn from_options(ai: &CodewikiAiOptions) -> Self {
        Self {
            prose_depth: match ai.prose_depth {
                ProseDepth::Standard => String::new(),
                ProseDepth::Brief => "brief".to_string(),
                ProseDepth::Deep => "deep".to_string(),
            },
            register: match ai.register {
                None => String::new(),
                Some(ProseRegister::Newcomer) => "newcomer".to_string(),
                Some(ProseRegister::Maintainer) => "maintainer".to_string(),
                Some(ProseRegister::Agent) => "agent".to_string(),
            },
            aggregate_profile: ai.aggregate_profile.clone().unwrap_or_default(),
            aggregate_candidates: ai
                .aggregate_candidates
                .iter()
                .map(FeatureCandidate::cli_label)
                .collect(),
        }
    }

    /// The subset of these settings that shaped `doc_path`: aggregate-writer
    /// pages carry the aggregate profile/candidate chain, every other page only
    /// the run-wide prose depth and register — so an aggregate flag change
    /// invalidates exactly the aggregate pages while file/module pages reuse.
    pub(crate) fn for_path(&self, doc_path: &str) -> Self {
        if aggregate_writer_page(doc_path) {
            self.clone()
        } else {
            Self {
                prose_depth: self.prose_depth.clone(),
                register: self.register.clone(),
                ..Self::default()
            }
        }
    }

    /// Whether `meta`'s recorded settings match these per-page expected
    /// settings. Callers pass the [`AiGenerationSettings::for_path`] projection
    /// for the page being compared.
    pub(crate) fn matches_meta(&self, meta: &CodewikiDocMeta) -> bool {
        meta.ai_prose_depth == self.prose_depth
            && meta.ai_register == self.register
            && meta.ai_aggregate_profile == self.aggregate_profile
            && meta.ai_aggregate_candidates == self.aggregate_candidates
    }
}

/// Pages whose prose comes from the aggregate-tier writer (`generate_aggregate`
/// / `PromptTier::Aggregate`): the repo overview, the architecture narrative,
/// and the curated concept/narrative pages (whose bodies and navigation plan
/// are aggregate-tier generations). `--ai-aggregate-profile` and
/// `--ai-aggregate-candidate` shape exactly these pages. Deterministic derived
/// pages (infrastructure, features, deprecations) render without an LLM and are
/// excluded.
fn aggregate_writer_page(doc_path: &str) -> bool {
    doc_path != CONCEPT_INDEX_DOC_PATH
        && (doc_path == REPO_DOC_PATH
            || doc_path == ARCHITECTURE_DOC_PATH
            || is_curated_doc(doc_path))
}

pub(crate) fn is_curated_doc(doc_path: &str) -> bool {
    doc_path.starts_with(CONCEPT_DOC_PREFIX) || doc_path.starts_with(NARRATIVE_DOC_PREFIX)
}

pub(crate) fn ai_outcome_for_doc(
    doc_path: &str,
    one_shot: CodewikiAiOutcome,
    aggregate: CodewikiAiOutcome,
) -> CodewikiAiOutcome {
    if doc_path != CONCEPT_INDEX_DOC_PATH
        && (doc_path == REPO_DOC_PATH
            || doc_path == ARCHITECTURE_DOC_PATH
            || is_curated_doc(doc_path))
    {
        aggregate
    } else {
        one_shot
    }
}

pub type TextGenerator<'a> = dyn FnMut(&str, &str, PromptTier) -> Option<String> + 'a;

/// Thread-safe generation call shared by the bounded file-page worker pool
/// (#17532). The resolved production generator is stateless apart from its
/// warn-once flag, so it is exposed as `Fn + Send + Sync`; serial call sites
/// adapt it to the [`TextGenerator`] `FnMut` surface with a local closure.
pub(crate) type SyncTextGenerator<'a> =
    dyn Fn(&str, &str, PromptTier) -> Option<String> + Send + Sync + 'a;

/// Grounded verification call: given a verify prompt and system prompt, returns
/// the raw model response, or `None` when the verifier is unavailable (routed
/// off, transport failure, or generation error). Callers treat `None` as "skip
/// verification, proceed undegraded". The deterministic block numbering,
/// response parsing, and stripping live in [`super::super::text`], so the
/// closure is just the model call — mirroring [`TextGenerator`] but without a
/// prompt tier.
pub type TextVerifier<'a> = dyn FnMut(&str, &str) -> Option<String> + 'a;

/// Thread-safe verification call for the bounded file-page worker pool,
/// mirroring [`SyncTextGenerator`] exactly as [`TextVerifier`] mirrors
/// [`TextGenerator`].
pub(crate) type SyncTextVerifier<'a> = dyn Fn(&str, &str) -> Option<String> + Send + Sync + 'a;

/// Weight tier of one codewiki generation call (#904). `Aggregate` is the
/// top-level repo-wide synthesis — repo overview, architecture, and the curated
/// narrative/concept layer — on the heavy feature_high profile. `Module` is mid-level
/// per-unit synthesis (module docs and file-body narratives) and routes to
/// sonnet. `Standard` is high-volume per-symbol prose on the default low tier.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum PromptTier {
    #[default]
    Standard,
    Module,
    Aggregate,
}

/// How deep AI prose generation reaches. Deeper tiers include shallower ones;
/// gated tiers fall back to structural summaries.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub enum AiDepth {
    /// Architecture, module, and repo prose only.
    Sections,
    /// Sections plus per-file summaries.
    #[default]
    Files,
    /// Files plus per-symbol purposes (one LLM call per symbol).
    Symbols,
}

impl AiDepth {
    pub(crate) fn includes_files(self) -> bool {
        self >= AiDepth::Files
    }

    pub(crate) fn includes_symbols(self) -> bool {
        self >= AiDepth::Symbols
    }

    pub(crate) fn mode_label(self) -> &'static str {
        match self {
            AiDepth::Sections => "sections",
            AiDepth::Files => "files",
            AiDepth::Symbols => "symbols",
        }
    }
}

/// Output verbosity for AI prose, orthogonal to [`AiDepth`] (which page tiers
/// reach the LLM) and to the audience register. Maps to a per-page output token
/// budget; [`ProseDepth::Standard`] defers to the provider/profile default so a
/// run without the flag is byte-identical to before this control existed.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub enum ProseDepth {
    /// Tighter pages: cap output low so prose stays terse.
    Brief,
    /// Provider/profile default budget (unchanged behavior).
    #[default]
    Standard,
    /// Richer pages: raise the output budget for longer explanations.
    Deep,
}

impl ProseDepth {
    /// Per-page output token budget, or `None` to defer to the provider/profile
    /// default. `Standard` returns `None` so the default run is unchanged;
    /// `Brief`/`Deep` pin a lower/higher ceiling.
    pub(crate) fn max_tokens(self) -> Option<usize> {
        match self {
            ProseDepth::Brief => Some(640),
            ProseDepth::Standard => None,
            ProseDepth::Deep => Some(2_400),
        }
    }
}

/// Audience register for AI prose, orthogonal to depth. Every register projects
/// the same grounded facts and only changes voice; `None` (the default) leaves
/// the base system prompts untouched so default runs are unchanged.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProseRegister {
    /// ELI5: plain language, defines jargon on first use, leads with the
    /// problem the code solves.
    Newcomer,
    /// Maintainer: leads with why the code is shaped this way and the
    /// non-obvious trade-offs.
    Maintainer,
    /// Build substrate: terse decisions and structure, minimal connective prose.
    Agent,
}

/// Which pages run the grounded verification pass. The aggregate/curated
/// pages always verify; this gates the expensive per-file-leaf verification,
/// which dominates verify cost on large repos.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum VerifyScope {
    /// Verify the aggregate/curated pages; skip per-file-leaf verification.
    #[default]
    Aggregates,
    /// Verify every generated page, including per-file leaves.
    All,
}

impl VerifyScope {
    /// Whether per-file-leaf pages run the grounded verification pass.
    pub fn verifies_leaves(self) -> bool {
        matches!(self, Self::All)
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CodewikiAiOptions {
    pub routing: Option<AiRouting>,
    pub depth: AiDepth,
    /// Output verbosity (per-page token budget). Default keeps prior behavior.
    pub prose_depth: ProseDepth,
    /// Audience register layered onto generation prompts. `None` keeps the base
    /// voice; grounding rules hold in every register.
    pub register: Option<ProseRegister>,
    /// Daemon feature profile override for aggregate docs. `None` (the default)
    /// routes aggregate/curated writing through the standard generate path with
    /// the binding's default profile (see `text/generation.rs`); `Some(profile)`
    /// pins that named daemon feature profile instead.
    pub aggregate_profile: Option<String>,
    /// Explicit provider/model candidate chain pinning the aggregate writer
    /// (`--ai-aggregate-candidate`, repeatable and ordered). Empty (the
    /// default) leaves profile-based routing in charge; non-empty entries are
    /// forwarded via the daemon request's `candidates` field, superseding the
    /// profile. Daemon route only — the Direct route rejects explicit
    /// candidates with a hard error. Mutually exclusive with
    /// `aggregate_profile` (enforced at the CLI).
    pub aggregate_candidates: Vec<FeatureCandidate>,
    /// Override seams for the grounded verification pass. Each `None` falls
    /// back to the resolved `ai.text_generate.verify_*` config, then to the
    /// generate model/key and [`super::super::DEFAULT_VERIFY_PROFILE`]. Kept
    /// here so the generator set is resolved from one options value and the
    /// precedence is unit-testable.
    pub verify_profile: Option<String>,
    pub verify_model: Option<String>,
    pub verify_api_key: Option<String>,
    /// Which pages run the grounded verification pass. Default
    /// ([`VerifyScope::Aggregates`]) verifies the aggregate/curated pages and
    /// skips the expensive per-file-leaf verification; [`VerifyScope::All`]
    /// restores leaf verification.
    pub verify_scope: VerifyScope,
}
