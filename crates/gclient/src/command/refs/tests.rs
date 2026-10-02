use super::*;
use serde_json::json;

const TAB: &str = "7a000000-0000-4000-8000-000000000001";
const PANE_A: &str = "ab120000-0000-4000-8000-000000000002";
const PANE_B: &str = "ab340000-0000-4000-8000-000000000003";

/// Workspace 0:2 with one tab and two panes whose ids share the prefix `ab`.
fn snapshot() -> WorkspaceSnapshot {
    let pane = |id: &str, reference: u64| {
        json!({"id": id, "tab_id": TAB, "ref": reference, "terminal_id": null,
               "owns_terminal": true, "label": null, "created_at": "", "updated_at": ""})
    };
    serde_json::from_value(json!({
        "workspace": {"id": "workspace-id", "machine_id": "machine", "ref": 2, "name": "default",
                      "focused_project_id": null, "focused_tab_id": null,
                      "created_at": "", "updated_at": "", "node_ref": 0},
        "tabs": [{"id": TAB, "workspace_id": "workspace-id", "ref": 1, "title": null,
                  "project_id": "project", "worktree_id": null, "position": 0,
                  "focused_pane_id": null, "layout": {"kind": "pane", "pane_id": PANE_A},
                  "created_at": "", "updated_at": ""}],
        "panes": [pane(PANE_A, 0), pane(PANE_B, 1)],
        "snapshot": {"daemon_epoch": "epoch", "seq": 1},
    }))
    .expect("snapshot fixture")
}

fn target(reference: &str, rows: Rows) -> Target {
    Target::new(reference, Some("default".into()), None, rows)
        .expect("target")
        .expect("checked")
}

#[test]
fn short_ids_are_uuid_prefixes_not_refs() {
    assert!(is_short_id("ab12"));
    assert!(is_short_id("AB12-00"));
    assert!(!is_short_id("0:2:1:0"));
    assert!(!is_short_id("12"));
    assert!(!is_short_id(PANE_A));
    assert!(!is_short_id("default"));
    assert!(!is_short_id(""));
}

#[test]
fn full_ref_without_explicit_workspace_goes_to_the_daemon_unchecked() {
    let workspace = Some("default".to_owned());
    assert!(
        Target::new("0:9:1:0", None, workspace.as_ref(), Rows::Panes)
            .unwrap()
            .is_none()
    );
}

#[test]
fn short_id_without_any_workspace_is_a_usage_error() {
    let error = Target::new("ab12", None, None, Rows::Panes).unwrap_err();
    assert_eq!(error.code, 2);
    assert!(error
        .message
        .contains("needs --workspace REF or GOBBY_WORKSPACE_ID"));
}

#[test]
fn short_id_falls_back_to_the_pane_workspace() {
    let workspace = Some("0:2".to_owned());
    let target = Target::new("ab12", None, workspace.as_ref(), Rows::Panes)
        .unwrap()
        .unwrap();
    assert_eq!(target.workspace, "0:2");
}

#[test]
fn unambiguous_prefix_resolves_to_its_pane() {
    assert_eq!(
        target("AB34", Rows::Panes).resolve(&snapshot()).unwrap(),
        Some(Row::Pane {
            id: PANE_B.into(),
            tab_id: TAB.into()
        })
    );
    assert_eq!(
        target("7a", Rows::Either).resolve(&snapshot()).unwrap(),
        Some(Row::Tab { id: TAB.into() })
    );
}

#[test]
fn ambiguous_prefix_lists_every_candidate() {
    let error = target("ab", Rows::Panes).resolve(&snapshot()).unwrap_err();
    assert_eq!(error.code, 2);
    assert!(error
        .message
        .contains("short ID ab is ambiguous in workspace default"));
    assert!(error.message.contains(&format!("pane {PANE_A}")));
    assert!(error.message.contains(&format!("pane {PANE_B}")));
}

#[test]
fn unmatched_prefix_names_the_workspace() {
    let error = target("ff", Rows::Panes).resolve(&snapshot()).unwrap_err();
    assert_eq!(error.code, 1);
    assert_eq!(error.message, "no pane in workspace default starts with ff");
    let error = target("7a", Rows::Panes).resolve(&snapshot()).unwrap_err();
    assert_eq!(error.message, "no pane in workspace default starts with 7a");
}

#[test]
fn explicit_workspace_bounds_a_full_ref() {
    for inside in ["0:2:1:0", "0:2:1", PANE_A, TAB] {
        assert_eq!(
            target(inside, Rows::Either).resolve(&snapshot()).unwrap(),
            None,
            "{inside}"
        );
    }
    for outside in ["0:3:1:0", "1:2:1:0", "cd000000-0000-4000-8000-000000000009"] {
        let error = target(outside, Rows::Either)
            .resolve(&snapshot())
            .unwrap_err();
        assert_eq!(error.code, 2, "{outside}");
        assert_eq!(
            error.message,
            format!("{outside} is not in workspace default")
        );
    }
}

#[test]
fn retarget_aims_the_op_at_the_resolved_row() {
    let pane = Row::Pane {
        id: PANE_A.into(),
        tab_id: TAB.into(),
    };
    let tab = Row::Tab { id: TAB.into() };
    let read = WorkspaceOp::PaneRead {
        pane: "ab12".into(),
        lines: None,
        node: None,
    };
    assert!(matches!(
        retarget(read, pane.clone()),
        WorkspaceOp::PaneRead { pane, .. } if pane == PANE_A
    ));
    let close = WorkspaceOp::PaneClose {
        pane: "7a".into(),
        node: None,
    };
    assert!(matches!(
        retarget(close, tab.clone()),
        WorkspaceOp::TabClose { tab, .. } if tab == TAB
    ));
    let rename = WorkspaceOp::PaneRename {
        pane: "7a".into(),
        label: Some("named".into()),
        node: None,
    };
    assert!(matches!(
        retarget(rename, tab),
        WorkspaceOp::TabRename { tab, title: Some(title), .. } if tab == TAB && title == "named"
    ));
    let select = WorkspaceOp::WorkspaceSelect {
        workspace: "default".into(),
        tab: "7a".into(),
        pane: None,
        node: None,
    };
    assert!(matches!(
        retarget(select, pane),
        WorkspaceOp::WorkspaceSelect { tab, pane: Some(pane), .. }
            if tab == TAB && pane == PANE_A
    ));
}
