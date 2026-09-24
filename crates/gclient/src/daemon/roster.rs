//! The attention roster's typed rows (`GET /api/attention/roster`): one entry per
//! session or run, with its terminal, its task and its blocked half.

use crate::app::Backend;
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
pub struct RosterEntry {
    pub entry_id: String,
    pub run_id: Option<String>,
    pub session_id: Option<String>,
    pub lifecycle_status: Option<String>,
    /// Set only while the subject is blocked; the daemon sends `null` otherwise.
    pub attention: Option<Attention>,
    pub task: Option<TaskRef>,
    pub provider: Option<String>,
    pub model: Option<String>,
    /// The model's name as its provider prints it (`Claude Fable 5.1`),
    /// resolved by the daemon from its capability rows; absent when no row
    /// matches, and the chrome then shows the raw selector.
    #[serde(default)]
    pub model_display_name: Option<String>,
    pub terminal: Option<TerminalRef>,
    #[serde(default, rename = "tmux", deserialize_with = "tmux_session_name")]
    pub tmux_session_name: Option<String>,
    pub last_activity_at: Option<String>,
    /// How full the subject's context window is, for the status bar.
    #[serde(default)]
    pub context_percent: Option<u8>,
    /// Tokens the subject has used, for the status bar.
    #[serde(default)]
    pub tokens_used: Option<u64>,
}

/// The blocked half of a roster entry (`_serialize_attention`).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Attention {
    pub attention_id: Option<String>,
    pub kind: Option<String>,
    pub reason: Option<String>,
    pub fingerprint: Option<String>,
    #[serde(alias = "prompt")]
    pub payload: Option<Value>,
    pub seen_at: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct TaskRef {
    pub id: String,
    #[serde(rename = "ref")]
    pub reference: Option<String>,
    /// A run's task title, or the title of the open task a session holds.
    pub title: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct TerminalRef {
    pub terminal_id: String,
    pub backend: Backend,
    /// The terminal row's lifecycle state (`live`, `orphaned`, ...); an
    /// `orphaned` row lost its host and can only be destroyed.
    pub state: Option<String>,
}

/// The daemon nests the tmux name as `tmux: {session_name}`; a `null` block
/// means the terminal is not tmux.
fn tmux_session_name<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    struct Tmux {
        session_name: Option<String>,
    }
    Ok(Option::<Tmux>::deserialize(deserializer)?.and_then(|tmux| tmux.session_name))
}
