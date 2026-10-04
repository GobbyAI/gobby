use super::*;
use crate::app::pane::{Backend, PaneId};
use crate::frame_source::{PaneFrameSource, ScriptedFrameSource};
use crate::theme::{Theme, ThemeKind};
use gobby_terminal::terminal_theme::{DefaultColorKind, RgbColor};
use serde_json::json;
use std::time::Instant;

fn declaration(kind: ThemeKind) -> ThemeDeclaration {
    (&Theme::new(kind).terminal_theme()).into()
}

/// An attached pane on a scripted direct stream whose host advertised themes.
fn capable_pane() -> Pane {
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.host_themes = true;
    pane
}

fn sent(pane: &Pane) -> Vec<ClientMessage> {
    pane.frame_source
        .as_ref()
        .and_then(PaneFrameSource::scripted)
        .expect("scripted source")
        .sent_messages()
        .to_vec()
}

#[test]
fn only_an_advertised_terminal_theme_capability_counts() {
    assert!(host_accepts_themes(
        &json!({"host_capabilities": ["other", "terminal_theme"]})
    ));
    assert!(!host_accepts_themes(&json!({"host_capabilities": []})));
    assert!(!host_accepts_themes(
        &json!({"host_capabilities": "terminal_theme"})
    ));
    assert!(!host_accepts_themes(&json!({})));
}

/// The query asks the hosting terminal for its default colours alone: the
/// palette stays gclient's, so 256 OSC 4 answers would be noise.
#[test]
fn host_color_query_asks_for_the_default_colours() {
    let mut output = Vec::new();
    query_host_colors(&HostColorQueryArm::default(), &mut output).unwrap();
    assert_eq!(output, b"\x1b]10;?\x1b\\\x1b]11;?\x1b\\");
}

#[test]
fn a_capable_pane_binds_then_declares_each_new_theme_once() {
    let mut pane = capable_pane();
    let attachment_id = pane.attachment_id().to_string();
    let dark = declaration(ThemeKind::Dark);
    let light = declaration(ThemeKind::Light);

    pane.sync_terminal_theme(&dark);
    pane.sync_terminal_theme(&dark);
    pane.sync_terminal_theme(&light);

    assert_eq!(
        sent(&pane),
        vec![
            ClientMessage::BindAttachment { attachment_id },
            ClientMessage::SetTerminalTheme {
                theme: dark.clone()
            },
            ClientMessage::SetTerminalTheme {
                theme: light.clone()
            },
        ]
    );
    assert_eq!(pane.declared_theme, Some(light));
}

#[test]
fn a_host_without_the_capability_is_sent_nothing() {
    let mut pane = capable_pane();
    pane.host_themes = false;

    pane.sync_terminal_theme(&declaration(ThemeKind::Dark));

    assert!(sent(&pane).is_empty());
    assert_eq!(pane.declared_theme, None);
}

#[test]
fn a_proxied_pane_is_sent_nothing() {
    let mut pane = capable_pane();
    pane.frame_source = Some(PaneFrameSource::Scripted(ScriptedFrameSource::new(
        Transport::Proxy,
    )));

    pane.sync_terminal_theme(&declaration(ThemeKind::Dark));

    assert!(sent(&pane).is_empty());
}

fn proxied_pane(host_themes: bool) -> Pane {
    let mut pane = capable_pane();
    pane.host_themes = host_themes;
    pane.frame_source = Some(PaneFrameSource::Scripted(ScriptedFrameSource::new(
        Transport::Proxy,
    )));
    pane
}

fn relayed(ws: &Workspace<crate::daemon::ScriptedDaemon>) -> Vec<serde_json::Value> {
    ws.daemon()
        .ws_sent()
        .into_iter()
        .filter(|message| message["type"] == "terminal_set_theme")
        .collect()
}

#[tokio::test]
async fn a_proxied_pane_is_relayed_through_the_daemon_once_per_theme() {
    let mut ws = Workspace::scripted();
    let pane = proxied_pane(true);
    let attachment_id = pane.attachment_id().to_string();
    ws.panes.insert(PaneId(1), pane);
    let dark = declaration(ThemeKind::Dark);

    ws.sync_terminal_themes(&dark).await;
    ws.sync_terminal_themes(&dark).await;

    assert_eq!(
        relayed(&ws),
        vec![json!({
            "type": "terminal_set_theme",
            "terminal_id": "terminal-1",
            "attachment_id": attachment_id,
            "theme": dark,
        })]
    );
    assert!(
        sent(&ws.panes[&PaneId(1)]).is_empty(),
        "nothing on the frame source"
    );
}

#[tokio::test]
async fn a_proxied_pane_without_the_relay_is_sent_nothing() {
    let mut ws = Workspace::scripted();
    ws.panes.insert(PaneId(1), proxied_pane(false));

    ws.sync_terminal_themes(&declaration(ThemeKind::Dark)).await;

    assert!(relayed(&ws).is_empty());
}

#[test]
fn a_new_frame_source_redeclares_only_after_its_host_advertises() {
    let mut pane = capable_pane();
    let dark = declaration(ThemeKind::Dark);
    pane.sync_terminal_theme(&dark);

    pane.install_frame_source(PaneFrameSource::Scripted(ScriptedFrameSource::new(
        Transport::Direct,
    )));
    pane.sync_terminal_theme(&dark);
    assert!(
        sent(&pane).is_empty(),
        "no capability on the new stream yet"
    );

    pane.host_themes = true;
    pane.sync_terminal_theme(&dark);
    assert!(matches!(
        sent(&pane).as_slice(),
        [
            ClientMessage::BindAttachment { .. },
            ClientMessage::SetTerminalTheme { .. }
        ]
    ));
}

#[test]
fn a_detached_pane_is_sent_nothing() {
    let mut pane = Pane::new_detached(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.host_themes = true;

    pane.sync_terminal_theme(&declaration(ThemeKind::Dark));

    assert_eq!(pane.declared_theme, None);
}

/// System declares the hosting terminal's own ground, and that terminal can
/// answer before it repaints after an appearance flip. The first answer is
/// then stale, so System asks again on a cadence, and the later answer is the
/// one panes are told (#23286 close review 88b326a0).
#[test]
fn system_asks_again_so_a_stale_host_answer_is_replaced() {
    let arm = HostColorQueryArm::default();
    let query = HOST_COLOR_QUERY_SEQUENCE.as_bytes();
    let mut chrome = crate::ui::chrome::Chrome::dark();
    chrome.prefs.theme = "System".to_string();
    let mut asked_at = None;
    let mut output = Vec::new();
    let flip = Instant::now();

    assert!(query_host_colors_when_due(&arm, &mut asked_at, true, flip, &mut output).unwrap());
    let stale = RgbColor {
        r: 0xef,
        g: 0xf1,
        b: 0xf5,
    };
    chrome.record_host_color(DefaultColorKind::Background, stale);
    assert_eq!(chrome.terminal_theme().background, Some(stale));

    let soon = flip + HOST_COLOR_REQUERY_INTERVAL / 2;
    assert!(!query_host_colors_when_due(&arm, &mut asked_at, false, soon, &mut output).unwrap());
    assert_eq!(output, query, "no second ask inside the interval");

    let later = flip + HOST_COLOR_REQUERY_INTERVAL;
    assert!(query_host_colors_when_due(&arm, &mut asked_at, false, later, &mut output).unwrap());
    assert_eq!(
        output,
        [query, query].concat(),
        "asked again after the interval"
    );
    let repainted = RgbColor {
        r: 0x1e,
        g: 0x1e,
        b: 0x2e,
    };
    chrome.record_host_color(DefaultColorKind::Background, repainted);
    assert_eq!(chrome.terminal_theme().background, Some(repainted));
}
