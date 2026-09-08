use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::daemon::{Answer, Daemon, DaemonError, LiveDaemon, RosterEntry};
use crate::ui::dialogs::Dialog;
use crate::ui::{Chrome, Mode};

use super::Workspace;

#[derive(Clone, Debug)]
pub(super) struct PendingAttention {
    entry_id: String,
    attention_id: String,
    fingerprint: String,
    option_values: Vec<u64>,
}

struct Prompt {
    pending: PendingAttention,
    prompt: String,
    options: Vec<String>,
}

/// Open the response dialog for the first actionable prompt among the known
/// attention entries, or for `entry_id` alone when its row was clicked.
pub(super) async fn open_response_dialog(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    entry_id: Option<&str>,
) -> Result<(), DaemonError> {
    let known_entries = workspace.attention_entry_ids();
    let prompt = workspace
        .daemon()
        .roster()
        .await?
        .into_iter()
        .filter(|entry| known_entries.iter().any(|known| known == &entry.entry_id))
        .filter(|entry| entry_id.is_none_or(|wanted| wanted == entry.entry_id))
        .find_map(parse_prompt);
    let Some(prompt) = prompt else {
        chrome.status_message = Some("No actionable attention prompt".to_string());
        return Ok(());
    };

    chrome.dialog = Some(Dialog::Respond {
        entry_id: prompt.pending.entry_id.clone(),
        prompt: prompt.prompt,
        options: prompt.options,
        selected: 0,
        text: String::new(),
    });
    chrome.mode = Mode::Respond;
    workspace.pending_attention = Some(prompt.pending);
    Ok(())
}

pub(super) async fn route_response_input(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    key: &KeyEvent,
) -> Result<(), DaemonError> {
    if !matches!(chrome.dialog, Some(Dialog::Respond { .. })) {
        chrome.mode = Mode::Terminal;
        workspace.pending_attention = None;
        return Ok(());
    }

    match key.code {
        KeyCode::Esc => {
            chrome.dialog = None;
            chrome.mode = Mode::Terminal;
            workspace.pending_attention = None;
        }
        KeyCode::Up => adjust_selection(chrome, -1),
        KeyCode::Down => adjust_selection(chrome, 1),
        KeyCode::Backspace => {
            if let Some(Dialog::Respond { text, .. }) = &mut chrome.dialog {
                text.pop();
            }
        }
        KeyCode::Char(character)
            if !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
        {
            if let Some(Dialog::Respond { options, text, .. }) = &mut chrome.dialog {
                if options.is_empty() {
                    text.push(character);
                }
            }
        }
        KeyCode::Enter => submit_response(workspace, chrome).await?,
        _ => {}
    }
    Ok(())
}

fn adjust_selection(chrome: &mut Chrome, delta: isize) {
    let Some(Dialog::Respond {
        options, selected, ..
    }) = &mut chrome.dialog
    else {
        return;
    };
    if options.is_empty() {
        return;
    }
    if delta < 0 {
        *selected = selected.saturating_sub(1);
    } else {
        *selected = (*selected + 1).min(options.len() - 1);
    }
}

async fn submit_response(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), DaemonError> {
    let Some(pending) = workspace.pending_attention.clone() else {
        return Ok(());
    };
    let Some(Dialog::Respond { selected, text, .. }) = &chrome.dialog else {
        return Ok(());
    };
    let answer = if let Some(option) = pending.option_values.get(*selected) {
        Answer::option(&pending.fingerprint, *option)
    } else if !text.is_empty() {
        Answer::text(&pending.fingerprint, text)
    } else {
        return Ok(());
    };

    workspace
        .daemon()
        .respond(&pending.entry_id, &pending.attention_id, &answer)
        .await?;
    workspace.pending_attention = None;
    chrome.dialog = None;
    chrome.mode = Mode::Terminal;
    chrome.status_message = Some("Response sent".to_string());
    Ok(())
}

fn parse_prompt(entry: RosterEntry) -> Option<Prompt> {
    let attention = entry.attention?;
    if attention
        .kind
        .as_deref()
        .is_some_and(|kind| kind != "actionable")
    {
        return None;
    }
    let attention_id = attention.attention_id?;
    let fingerprint = attention.fingerprint?;
    let payload = attention.payload?;
    let prompt = payload
        .get("prompt")
        .or_else(|| payload.get("question"))
        .or_else(|| payload.get("excerpt"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or("Attention requires a response")
        .to_string();

    let mut labels = Vec::new();
    let mut values = Vec::new();
    for (index, option) in payload
        .get("options")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
        .enumerate()
    {
        let value = option
            .get("option")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or(index as u64 + 1);
        let label = option
            .get("label")
            .and_then(serde_json::Value::as_str)
            .or_else(|| option.as_str())
            .unwrap_or("Option")
            .to_string();
        values.push(value);
        labels.push(label);
    }

    Some(Prompt {
        pending: PendingAttention {
            entry_id: entry.entry_id,
            attention_id,
            fingerprint,
            option_values: values,
        },
        prompt,
        options: labels,
    })
}
