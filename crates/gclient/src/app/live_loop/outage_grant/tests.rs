use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use serde_json::json;

use super::*;

#[test]
fn control_paths_sit_beside_the_frame_socket() {
    let (socket, token) = control_paths(Path::new("/home/josh/.gobby/gterm-frames.sock"));
    assert_eq!(socket, Path::new("/home/josh/.gobby/gterm-control.sock"));
    assert_eq!(token, Path::new("/home/josh/.gobby/gterm-control.token"));
}

#[test]
fn owner_only_accepts_owner_modes_and_refuses_any_group_or_other_bit() {
    let dir = tempfile::tempdir().expect("token dir");
    let path = dir.path().join("gterm-control.token");
    std::fs::write(&path, "token").expect("write token");
    for (mode, accepted) in [
        (0o600, true),
        (0o400, true),
        (0o640, false),
        (0o604, false),
        (0o666, false),
    ] {
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode))
            .expect("set token mode");
        assert_eq!(
            verify_owner_only(&path).is_ok(),
            accepted,
            "mode {mode:o} is owner-only: {accepted}"
        );
    }
}

#[test]
fn owner_only_refuses_a_missing_file() {
    let dir = tempfile::tempdir().expect("token dir");
    assert!(verify_owner_only(&dir.path().join("gterm-control.token")).is_err());
}

#[test]
fn grant_request_names_the_terminal_and_the_attachment() {
    assert_eq!(
        grant_request("grant-1", "ht-1", "att-1", 42),
        json!({
            "attachment_id": "att-1",
            "host_attachment_id": 42,
            "host_terminal_id": "ht-1",
            "id": "grant-1",
            "method": "grant_input",
        })
    );
}

#[test]
fn revoke_request_clears_only_the_named_holder() {
    assert_eq!(
        revoke_request("revoke-1", "ht-1", "att-1"),
        json!({
            "attachment_id": "att-1",
            "host_terminal_id": "ht-1",
            "id": "revoke-1",
            "method": "revoke_input",
        })
    );
}

#[test]
fn grant_outcome_reads_a_grant_and_each_refusal() {
    assert_eq!(
        grant_outcome(&json!({"id": "g", "ok": true, "granted": true, "previous": null})),
        GrantOutcome::Granted
    );
    assert_eq!(
        grant_outcome(&json!({"id": "g", "ok": false, "error": "terminal_gone"})),
        GrantOutcome::Refused("terminal_gone".into())
    );
    assert_eq!(
        grant_outcome(&json!({"id": "g", "ok": true, "granted": false})),
        GrantOutcome::Refused("not_granted".into())
    );
}
