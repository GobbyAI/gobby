//! Token-budget helpers for gcode result trimming.
//!
//! The generic estimate/trim/hint logic now lives in
//! [`gobby_core::token_budget`] so `gcode` and `gwiki` share one
//! implementation. This module re-exports it under the historical
//! `crate::commands::token_budget` path used across the command handlers.

pub(crate) use gobby_core::token_budget::*;
