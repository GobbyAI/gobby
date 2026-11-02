use super::*;

fn tab_create(rows: Option<u16>, cols: Option<u16>) -> WorkspaceOp {
    WorkspaceOp::TabCreate {
        workspace: "workspace-1".into(),
        project_id: "project-1".into(),
        worktree_id: None,
        title: None,
        terminal_id: None,
        cwd: None,
        rows,
        cols,
        node: None,
    }
}

#[test]
fn tab_create_sends_the_viewport_it_was_given() {
    let request = op_request(&tab_create(Some(30), Some(100))).expect("request");

    assert_eq!(request["type"], json!("workspace_op"));
    assert_eq!(request["rows"], json!(30));
    assert_eq!(request["cols"], json!(100));
}

#[test]
fn tab_create_without_a_viewport_sends_neither_size() {
    let request = op_request(&tab_create(None, None)).expect("request");

    assert!(request.get("rows").is_none());
    assert!(request.get("cols").is_none());
}
