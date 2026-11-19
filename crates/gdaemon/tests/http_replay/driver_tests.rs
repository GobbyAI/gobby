//! Driver tests through `observe` and `classify` (fix3 review rounds V22, V23, V25 and V30).
//!
//! Each observation runs a real transport: the CLI case runs a probe script in place of the
//! `gobby` entry point, the WS case goes through the front door to a stub, and a hop between them
//! rewrites or adds bytes. Each test would fail if an observer were replaced by an echo of the
//! recording, because the observed value must come from the transport.

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use gobby_daemon::front_door::waivers::WaiverManifest;
use serde_json::{Value, json};

use super::backend::WsHop;
use super::corpus::parse_case;
use super::{
    Case, DriverError, Observation, Replay, TEXT_FIN, Transports, classify, frame, no_waivers,
    observe, open_window, v3_case,
};

/// Flips the first byte of each client chunk: a rewrite that keeps the length of the bytes.
fn flip_first_byte(bytes: &mut [u8]) {
    if let Some(first) = bytes.first_mut() {
        *first ^= 0x01;
    }
}

/// An executable `gobby` stand-in whose only output is `text` and a zero exit.
fn probe_script(dir: &Path, text: &str) -> PathBuf {
    let path = dir.join("gobby-probe");
    std::fs::write(&path, format!("#!/bin/sh\nprintf '%s\\n' '{text}'\n")).expect("write probe");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).expect("chmod probe");
    path
}

/// A recorded case cited by a synthetic waiver whose surface is `surface`, with `native` as its
/// native answer. `native_extension` cases drop their recorded `response`, as the loader requires.
fn cited(
    file: &str,
    waiver: &str,
    surface: &str,
    native: Value,
    native_extension: bool,
) -> (Case, WaiverManifest) {
    let (mut value, _) = v3_case(file);
    if native_extension {
        value
            .as_object_mut()
            .expect("case object")
            .shift_remove("response");
    }
    value["waiver"] = json!(waiver);
    value["native_response"] = native;
    let case = parse_case(&value).expect("cited case parses");
    let manifest = json!({
        "schema_version": 1,
        "waivers": [{
            "id": waiver,
            "surface": surface,
            "behaviour": "The case answers its native contract.",
            "decision": "#25938",
            "replacement": "The asserted native_response."
        }]
    });
    let waivers = WaiverManifest::parse(&manifest.to_string()).expect("synthetic waiver manifest");
    (case, waivers)
}

fn observation(response: Value) -> Observation {
    Observation {
        response,
        db_delta: Vec::new(),
        side_effects: Vec::new(),
    }
}

#[tokio::test]
async fn a_ws_client_leg_rewritten_on_its_way_to_the_backend_is_refused() {
    let (_, case) = v3_case("ws_terminal_list.json");
    let home = tempfile::tempdir().expect("home");
    let mut transports = Transports::recorded();
    transports.ws_hop = Some(WsHop {
        rewrite_client: Some(flip_first_byte),
        ..WsHop::default()
    });
    match observe(&case, home.path(), &open_window(), &transports).await {
        Err(DriverError::Shape(detail)) => {
            assert!(detail.contains("client leg"), "{detail}");
        }
        other => panic!("a rewritten client leg must be refused, got {other:?}"),
    }
}

#[tokio::test]
async fn a_ws_client_leg_without_a_hop_is_the_invocation() {
    // Control for the rewrite test: the same case through the same stub, unchanged, classifies.
    let (_, case) = v3_case("ws_terminal_list.json");
    let home = tempfile::tempdir().expect("home");
    let observed = observe(&case, home.path(), &open_window(), &Transports::recorded())
        .await
        .expect("observe");
    assert_eq!(
        classify(&case, &no_waivers(), &observed, &open_window()).expect("classify"),
        Replay::Match
    );
}

#[tokio::test]
async fn a_cli_case_observes_the_stdout_of_its_entry_point() {
    let (_, case) = v3_case("cli_schema_help.json");
    let dir = tempfile::tempdir().expect("probe dir");
    let mut transports = Transports::recorded();
    transports.gobby = probe_script(dir.path(), "probe output");
    let home = tempfile::tempdir().expect("home");
    let observed = observe(&case, home.path(), &open_window(), &transports)
        .await
        .expect("observe");
    assert_eq!(observed.response["stdout"], json!("probe output\n"));
    assert_eq!(
        classify(&case, &no_waivers(), &observed, &open_window()).expect("classify"),
        Replay::Diff {
            pointer: "/response/stdout".to_owned()
        }
    );
}

/// A stub reply with the recording's two frames and the same normalized fields (`client_id` and
/// `daemon_epoch`), but a different item list, so the only difference is a value.
fn differing_stub_reply() -> Vec<String> {
    vec![
        json!({
            "client_id": "00000000-0000-4000-8000-000000000001",
            "conversation_ids": [],
            "latency": 0.0,
            "type": "connection_established",
            "user_id": "20000000-0000-4000-8000-000000000001"
        })
        .to_string(),
        json!({
            "items": [{"id": "other"}],
            "next_cursor": null,
            "request_id": null,
            "snapshot": {"daemon_epoch": "00000000-0000-4000-8000-000000000002", "seq": 0},
            "type": "terminal_list"
        })
        .to_string(),
    ]
}

#[tokio::test]
async fn a_ws_case_observes_the_reply_its_stub_sends() {
    let (_, case) = v3_case("ws_terminal_list.json");
    let home = tempfile::tempdir().expect("home");
    let stub_reply = differing_stub_reply();
    let mut transports = Transports::recorded();
    transports.ws_reply = Some(stub_reply.clone());
    let observed = observe(&case, home.path(), &open_window(), &transports)
        .await
        .expect("observe");
    assert_eq!(observed.response, json!(stub_reply));
    let replay = classify(&case, &no_waivers(), &observed, &open_window()).expect("classify");
    assert_eq!(
        replay,
        Replay::Diff {
            pointer: "/response/1/items".to_owned()
        }
    );
}

#[tokio::test]
async fn a_late_ws_frame_after_the_reply_is_observed_and_is_a_diff() {
    let (_, case) = v3_case("ws_terminal_list.json");
    let home = tempfile::tempdir().expect("home");
    let mut transports = Transports::recorded();
    transports.ws_hop = Some(WsHop {
        late_reply: frame(TEXT_FIN, br#"{"type":"late"}"#, false),
        ..WsHop::default()
    });
    let observed = observe(&case, home.path(), &open_window(), &transports)
        .await
        .expect("observe");
    assert_eq!(
        observed.response.as_array().map(Vec::len),
        Some(3),
        "the recording's two frames and the late frame"
    );
    let replay = classify(&case, &no_waivers(), &observed, &open_window()).expect("classify");
    assert!(matches!(replay, Replay::Diff { .. }), "{replay:?}");
}

#[test]
fn a_waiver_surface_of_another_kind_is_a_waiver_error() {
    // A WS case cited by an HTTP-shaped surface, and a CLI case cited by an HTTP-shaped surface.
    for (file, key) in [
        ("ws_terminal_list.json", "WS message terminal_list"),
        ("cli_schema_help.json", "CLI gobby schema"),
    ] {
        let (case, waivers) = cited(
            file,
            "probe.foreign.waived",
            "HTTP GET /api/terminals",
            json!([]),
            false,
        );
        let observed = observation(json!([]));
        let error =
            classify(&case, &waivers, &observed, &open_window()).expect_err("foreign surface");
        assert!(
            matches!(error, DriverError::Waiver(_)),
            "{file} ({key}): {error:?}"
        );
    }
}

#[test]
fn a_native_extension_citation_classifies_its_native_answer() {
    // The CLI case has no normalizers, so its native answer is compared as written.
    let native = json!({"exit": 0, "stdout": "native", "stderr": ""});
    let (case, waivers) = cited(
        "cli_schema_help.json",
        "probe.cli.native-extension",
        "CLI gobby schema",
        native.clone(),
        true,
    );
    let on_native =
        classify(&case, &waivers, &observation(native), &open_window()).expect("classify native");
    assert_eq!(
        on_native,
        Replay::NativeExtension {
            waiver: "probe.cli.native-extension".to_owned()
        }
    );
    let on_other = classify(
        &case,
        &waivers,
        &observation(json!({"exit": 0, "stdout": "other", "stderr": ""})),
        &open_window(),
    )
    .expect("classify other");
    assert!(matches!(on_other, Replay::Diff { .. }), "{on_other:?}");
}

#[test]
fn a_waived_case_without_its_native_answer_is_a_shape_error() {
    let (case, waivers) = cited(
        "cli_schema_help.json",
        "probe.cli.waived",
        "CLI gobby schema",
        json!({"exit": 0, "stdout": "native", "stderr": ""}),
        false,
    );
    let error = classify(
        &Case {
            native_response: None,
            ..case
        },
        &waivers,
        &observation(json!([])),
        &open_window(),
    )
    .expect_err("no native answer");
    assert!(matches!(error, DriverError::Shape(_)), "{error:?}");
}
