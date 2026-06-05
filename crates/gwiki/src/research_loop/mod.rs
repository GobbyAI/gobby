//! Autonomous research loop: model-driven plan/act/observe cycle over the wiki vault.

mod engine;
mod helpers;
mod types;

pub(crate) use engine::ResearchLoop;
pub(crate) use helpers::{model_system_prompt, parse_model_action, render_model_prompt};
pub(crate) use types::{
    ModelDecision, ModelRequest, NoteWriteOutcome, ResearchLoopConfig, ResearchLoopDeps,
    ResearchLoopEvent, ResearchLoopInput, ResearchLoopResult, ResearchModel, ResearchModelError,
    ResearchNoteWriter, ResearchObservation, SourceIngestor, WikiAsk, WikiRead, WikiSearch,
};
#[cfg(test)]
pub(crate) use types::{ResearchAction, ResearchLoopDepsBuilder};

#[cfg(test)]
mod tests;
