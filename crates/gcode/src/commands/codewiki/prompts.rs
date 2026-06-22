mod builders;
mod excerpts;
mod systems;
mod tables;
mod types;

pub(crate) use builders::with_register;
pub use builders::{
    architecture_narrative_prompt, architecture_prompt, concept_page_prompt, content_file_prompt,
    file_prompt, module_prompt, narrative_page_prompt, repo_prompt, symbol_prompt, verify_prompt,
};
pub(crate) use excerpts::{
    CONCEPT_PAGE_SOURCE_EXCERPTS, MAX_PROMPT_SOURCE_EXCERPTS, NARRATIVE_PAGE_SOURCE_EXCERPTS,
};
// Compatibility re-exports: no current library call site needs these limits
// directly, but they were part of the prior prompts module surface.
#[allow(unused_imports)]
pub(crate) use excerpts::{SOURCE_EXCERPT_MAX_CHARS, VERIFY_SOURCE_EXCERPTS};
// Compatibility re-exports: register guidance strings remain available as
// prompts::... constants even though with_register is their only active user.
#[allow(unused_imports)]
pub(crate) use systems::{
    AGENT_REGISTER_GUIDANCE, MAINTAINER_REGISTER_GUIDANCE, NEWCOMER_REGISTER_GUIDANCE,
};
pub use systems::{
    ARCHITECTURE_NARRATIVE_SYSTEM, ARCHITECTURE_SYSTEM, CONCEPT_PAGE_SYSTEM, CONTENT_FILE_SYSTEM,
    CURATED_NAVIGATION_SYSTEM, FILE_SYSTEM, MODULE_SYSTEM, NARRATIVE_PAGE_SYSTEM, REPO_SYSTEM,
    SYMBOL_SYSTEM, VERIFY_SYSTEM,
};
pub use types::{ChildSummary, PageEvidenceRow, SourceExcerpt, SymbolSummary};

#[cfg(test)]
use excerpts::{CHILD_SUMMARY_EXCERPT_MAX_CHARS, summary_excerpt};

#[cfg(test)]
mod tests;
