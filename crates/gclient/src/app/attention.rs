use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::daemon::{Answer, Daemon, DaemonError, LiveDaemon, RosterEntry};
use crate::ui::dialogs::Dialog;
use crate::ui::status::Toast;
use crate::ui::text::truncate_end;
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
    let entries = workspace
        .daemon()
        .roster()
        .await?
        .into_iter()
        .filter(|entry| known_entries.iter().any(|known| known == &entry.entry_id))
        .filter(|entry| entry_id.is_none_or(|wanted| wanted == entry.entry_id))
        .collect::<Vec<_>>();
    present_attention(chrome, &mut workspace.pending_attention, &entries);
    Ok(())
}

fn present_attention(
    chrome: &mut Chrome,
    pending_attention: &mut Option<PendingAttention>,
    entries: &[RosterEntry],
) {
    let prompt = entries.iter().cloned().find_map(parse_prompt);
    let Some(prompt) = prompt else {
        let toast = entries
            .iter()
            .find_map(blocked_attention_toast)
            .unwrap_or_else(|| Toast::warning("No actionable attention prompt."));
        chrome.notify(toast);
        return;
    };

    chrome.dialog = Some(Dialog::Respond {
        entry_id: prompt.pending.entry_id.clone(),
        prompt: prompt.prompt,
        options: prompt.options,
        selected: 0,
        text: String::new(),
    });
    chrome.mode = Mode::Respond;
    *pending_attention = Some(prompt.pending);
}

fn blocked_attention_toast(entry: &RosterEntry) -> Option<Toast> {
    let attention = entry.attention.as_ref()?;
    if attention.kind.as_deref() != Some("non_actionable") {
        return None;
    }
    let raw_message = attention
        .payload
        .as_ref()
        .and_then(|payload| payload.get("message"))
        .and_then(serde_json::Value::as_str);
    let printable = raw_message
        .unwrap_or_default()
        .chars()
        .map(|character| {
            if character.is_control() {
                ' '
            } else {
                character
            }
        })
        .collect::<String>();
    let message = printable.split_whitespace().collect::<Vec<_>>().join(" ");
    let (title, body) = if message.is_empty() {
        ("Agent blocked", "No details were provided.".to_string())
    } else if attention.reason.as_deref() == Some("provider_error") {
        ("Provider error", truncate_end(&message, 80))
    } else {
        ("Agent blocked", truncate_end(&message, 80))
    };
    Some(Toast::warning(title).with_body(body))
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
    chrome.notify(Toast::success("Response sent."));
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::Attention;
    use crate::ui::status::ToastKind;
    use serde_json::json;

    fn blocked_entry(reason: &str, payload: serde_json::Value) -> RosterEntry {
        RosterEntry {
            entry_id: "run:provider-error".to_string(),
            attention: Some(Attention {
                kind: Some("non_actionable".to_string()),
                reason: Some(reason.to_string()),
                payload: Some(payload),
                ..Attention::default()
            }),
            ..RosterEntry::default()
        }
    }

    #[test]
    fn provider_error_shows_sanitized_warning_without_opening_response() {
        let mut chrome = Chrome::dark();
        let mut pending = Some(PendingAttention {
            entry_id: "run:prior".to_string(),
            attention_id: "prior".to_string(),
            fingerprint: "prior".to_string(),
            option_values: vec![],
        });
        let message = format!("API Error: 500\x1b[31m\n{}", "server issue ".repeat(10));

        present_attention(
            &mut chrome,
            &mut pending,
            &[blocked_entry("provider_error", json!({"message": message}))],
        );

        assert!(chrome.dialog.is_none());
        assert_eq!(chrome.mode, Mode::Terminal);
        assert_eq!(
            pending.as_ref().map(|item| item.attention_id.as_str()),
            Some("prior")
        );
        let toast = chrome.alert_log.last().expect("warning added to alert log");
        assert_eq!(toast.kind, ToastKind::Warning);
        assert_eq!(toast.title, "Provider error");
        let body = toast.body.as_deref().expect("warning body");
        assert!(body.starts_with("API Error: 500"));
        assert!(!body.contains('\x1b'));
        assert!(!body.contains('\n'));
        assert!(body.ends_with('…'));
        assert!(crate::ui::text::display_width(body) <= 80);
    }

    #[test]
    fn missing_provider_message_has_safe_blocked_fallback() {
        let mut chrome = Chrome::dark();
        let mut pending = None;

        present_attention(
            &mut chrome,
            &mut pending,
            &[blocked_entry("provider_error", json!({"message": 500}))],
        );

        assert!(chrome.dialog.is_none());
        assert_eq!(chrome.mode, Mode::Terminal);
        assert!(pending.is_none());
        let toast = chrome.alert_log.last().expect("fallback warning");
        assert_eq!(toast.title, "Agent blocked");
        assert_eq!(toast.body.as_deref(), Some("No details were provided."));
    }
}
