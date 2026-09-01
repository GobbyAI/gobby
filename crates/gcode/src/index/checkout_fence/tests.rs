use uuid::Uuid;

use super::fence_error;

const PROJECT: &str = "0f1f5df6-7f37-4a7f-9115-5b473f22934e";

fn project() -> Uuid {
    Uuid::parse_str(PROJECT).expect("test project uuid")
}

#[test]
fn unregistered_project_is_checkout_required_with_init_and_rebind_hints() {
    let error = fence_error(&project(), "/repo/main", Vec::new());

    assert_eq!(error.code, "checkout_required");
    assert_eq!(error.exit_status, 2);
    assert!(error.message.contains(PROJECT), "{}", error.message);
    assert!(
        error.message.contains("gcode computed root /repo/main"),
        "{}",
        error.message
    );
    assert!(
        error
            .message
            .contains("no checkout registered on this machine"),
        "{}",
        error.message
    );
    let recovery = error.recovery.as_deref().expect("recovery hint");
    assert!(
        recovery.contains("`gobby init` in /repo/main"),
        "{recovery}"
    );
    assert!(
        recovery.contains(&format!("gobby projects rebind {PROJECT} /repo/main")),
        "{recovery}"
    );
}

#[test]
fn mismatched_root_lists_registered_roots_next_to_computed_root() {
    let registered = vec!["/repo/main".to_string(), "/repo/other".to_string()];
    let error = fence_error(&project(), "/repo/moved", registered);

    assert_eq!(error.code, "checkout_mismatch");
    assert_eq!(error.exit_status, 2);
    assert!(error.message.contains(PROJECT), "{}", error.message);
    assert!(
        error.message.contains("primary index root /repo/moved"),
        "{}",
        error.message
    );
    assert!(
        error
            .message
            .contains("(registered: /repo/main, /repo/other)"),
        "{}",
        error.message
    );
    let recovery = error.recovery.as_deref().expect("recovery hint");
    assert!(
        recovery.contains(&format!("gobby projects rebind {PROJECT} /repo/moved")),
        "{recovery}"
    );

    let payload = error.json_payload();
    assert_eq!(payload["error"], "checkout_mismatch");
    assert_eq!(payload["message"], error.message);
    assert_eq!(payload["recovery"], recovery);
}
