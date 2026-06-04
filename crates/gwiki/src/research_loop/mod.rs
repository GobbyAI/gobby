//! Autonomous research loop: model-driven plan/act/observe cycle over the wiki vault.

mod engine;
mod helpers;
mod types;

pub(crate) use engine::ResearchLoop;
pub(crate) use helpers::*;
pub(crate) use types::*;

#[cfg(test)]
mod tests;
