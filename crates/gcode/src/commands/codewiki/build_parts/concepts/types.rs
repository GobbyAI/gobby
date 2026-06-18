use serde::Deserialize;

use super::super::super::VerifyNote;

#[derive(Debug, Deserialize)]
pub(super) struct CuratedNavigationPlan {
    #[serde(default)]
    pub(super) concept_modules: Vec<ConceptModule>,
    #[serde(default)]
    pub(super) sections: Vec<ConceptSection>,
    #[serde(default)]
    pub(super) narrative_pages: Vec<NarrativePage>,
}

#[derive(Debug, Deserialize)]
pub(super) struct ConceptModule {
    #[serde(default)]
    pub(super) slug: String,
    pub(super) title: String,
    #[serde(default)]
    pub(super) summary: String,
    #[serde(default)]
    pub(super) modules: Vec<String>,
    #[serde(default)]
    pub(super) files: Vec<String>,
    /// Multi-section body from the per-page content pass. `#[serde(skip)]` so
    /// the structure-pass JSON parse ignores it (and extra model fields can't
    /// perturb deserialization); populated after normalization.
    #[serde(skip)]
    pub(super) body: Option<String>,
    /// True when the content pass was attempted and fell back to the structural
    /// body, so the page records the degradation honestly (review #1).
    #[serde(skip)]
    pub(super) body_degraded: bool,
    #[serde(skip)]
    pub(super) verify_notes: Vec<VerifyNote>,
}

#[derive(Debug, Deserialize)]
pub(super) struct ConceptSection {
    pub(super) title: String,
    #[serde(default)]
    pub(super) summary: String,
    #[serde(default)]
    pub(super) concepts: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub(super) struct NarrativePage {
    #[serde(default)]
    pub(super) slug: String,
    pub(super) title: String,
    #[serde(default)]
    pub(super) summary: String,
    #[serde(default)]
    pub(super) concepts: Vec<String>,
    #[serde(default)]
    pub(super) modules: Vec<String>,
    #[serde(default)]
    pub(super) files: Vec<String>,
    /// Multi-section chapter body from the per-page content pass; see
    /// [`ConceptModule::body`]. `#[serde(skip)]` for the same reason.
    #[serde(skip)]
    pub(super) body: Option<String>,
    #[serde(skip)]
    pub(super) body_degraded: bool,
    #[serde(skip)]
    pub(super) verify_notes: Vec<VerifyNote>,
}
