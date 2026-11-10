use serde_json::{Value, json};

use super::*;

fn policy_row(source: &str, enabled: bool, value: Value) -> RuleRow {
    RuleRow {
        name: "fixture-writer".into(),
        source: source.into(),
        enabled,
        definition: json!({
            "event": "turn_start",
            "effects": [{
                "type": "set_variable",
                "variable": "project_policy",
                "value": value
            }]
        }),
    }
}

fn unrelated_row() -> RuleRow {
    RuleRow {
        name: "fixture-unrelated".into(),
        source: "installed".into(),
        enabled: true,
        definition: json!({"effects": [{"type": "inject_context", "template": "x"}]}),
    }
}

#[test]
fn no_policy_writer_yields_no_thresholds_so_the_defaults_are_reported_as_such() {
    let rows = vec![unrelated_row()];
    assert_eq!(thresholds_from_rule_rows(&rows), Ok(None));
    assert_eq!(thresholds_from_rule_rows(&[]), Ok(None));
}

#[test]
fn installed_writer_overrides_only_the_keys_it_sets() {
    let rows = vec![policy_row(
        "installed",
        true,
        json!({"fleet_health": {"max_new_seats": 2}, "other": true}),
    )];
    let thresholds = thresholds_from_rule_rows(&rows)
        .expect("resolves")
        .expect("a fleet_health block");
    assert_eq!(thresholds.max_new_seats, 2);
    assert_eq!(thresholds.ramp_window_seconds, 900);
}

#[test]
fn writer_without_a_fleet_health_block_yields_no_thresholds() {
    let rows = vec![policy_row("installed", true, json!({"quiet_hours": "x"}))];
    assert_eq!(thresholds_from_rule_rows(&rows), Ok(None));
}

#[test]
fn a_policy_read_is_refused_while_the_previous_one_is_still_outstanding() {
    static IN_FLIGHT: AtomicBool = AtomicBool::new(false);
    let first = claim(&IN_FLIGHT).expect("the first read claims the slot");
    assert!(
        claim(&IN_FLIGHT).is_none(),
        "a second read must not park another thread"
    );
    drop(first);
    assert!(
        claim(&IN_FLIGHT).is_some(),
        "the slot frees when the read ends"
    );
}

#[test]
fn rule_rows_are_scoped_to_global_rows_and_the_named_project() {
    assert!(
        RULE_ROWS_SQL.contains("project_id IS NULL OR project_id = $1::text::uuid"),
        "{RULE_ROWS_SQL}"
    );
    assert!(
        RULE_ROWS_SQL.contains("deleted_at IS NULL"),
        "{RULE_ROWS_SQL}"
    );
}

#[test]
fn enabled_writers_that_disagree_are_refused() {
    let rows = vec![
        policy_row(
            "installed",
            true,
            json!({"fleet_health": {"max_new_seats": 2}}),
        ),
        policy_row(
            "installed",
            true,
            json!({"fleet_health": {"max_new_seats": 4}}),
        ),
    ];
    assert!(thresholds_from_rule_rows(&rows).is_err());
}

#[test]
fn a_writer_that_is_not_installed_is_refused() {
    let rows = vec![policy_row("custom", true, json!({}))];
    let error = thresholds_from_rule_rows(&rows).expect_err("custom writer refused");
    assert!(error.contains("installed"), "{error}");
}

#[test]
fn a_non_writer_project_row_sharing_an_installed_writers_name_is_refused() {
    // Mirrors the Python shadow check, which scans every registry row by name, not only writers.
    let shadow = RuleRow {
        definition: json!({"event": "turn_start", "effects": []}),
        ..policy_row("project", false, json!({}))
    };
    let rows = vec![
        policy_row(
            "installed",
            true,
            json!({"fleet_health": {"max_new_seats": 2}}),
        ),
        shadow,
    ];
    let error = thresholds_from_rule_rows(&rows).expect_err("shadowing row refused");
    assert!(error.contains("not installed"), "{error}");
}

#[test]
fn a_disabled_writer_value_is_not_used_for_resolution() {
    let rows = vec![
        policy_row(
            "installed",
            true,
            json!({"fleet_health": {"max_new_seats": 2}}),
        ),
        policy_row(
            "installed",
            false,
            json!({"fleet_health": {"max_new_seats": 9}}),
        ),
    ];
    assert_eq!(
        thresholds_from_rule_rows(&rows)
            .expect("resolves")
            .expect("a fleet_health block")
            .max_new_seats,
        2
    );
}

#[test]
fn malformed_fleet_health_block_is_refused() {
    let rows = vec![policy_row(
        "installed",
        true,
        json!({"fleet_health": {"max_new_seat": 2}}),
    )];
    assert!(thresholds_from_rule_rows(&rows).is_err());
}
