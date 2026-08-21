//! 3.3.5 select → spawn → attach → terminate against scripted endpoints.

use gobby_client::Workspace;
use serde_json::json;

#[test]
fn select_spawn_attach_terminate_loop() {
    let mut ws = Workspace::scripted();
    ws.select_project("proj-1");
    assert_eq!(ws.project_id(), Some("proj-1"));

    ws.daemon_mut().set_spawn_response(json!({
        "success": true,
        "run_id": "run-1",
        "terminal_id": "term-spawn"
    }));
    ws.spawn_agent(json!({
        "task_id": "task-1",
        "agent_name": "default"
    }))
    .expect("spawn");
    let spawn_body = ws.daemon().last_spawn_body();
    assert!(
        spawn_body.get("backend").is_none(),
        "backend selection is 4.2: {spawn_body}"
    );
    let spawned = ws.pane_for_terminal("term-spawn").expect("reconciled pane");
    ws.attach_frames(spawned).expect("scripted frame attach");
    assert!(ws.pane(spawned).frames_rendered() >= 1);

    let keep = ws
        .open_terminal("term-keep", "native", "epoch-keep")
        .unwrap();
    ws.push_frame(keep, "keep-stream");
    assert!(ws.pane(keep).frames_rendered() >= 1);

    ws.terminate_terminal("term-spawn").expect("terminate");
    ws.apply_ws(&json!({
        "type": "terminal_event",
        "event": "exited",
        "terminal_id": "term-spawn"
    }))
    .unwrap();
    assert!(ws.pane_for_terminal("term-spawn").is_none());
    assert!(ws.pane(keep).is_live());
    ws.push_frame(keep, "still-streaming");
    assert!(ws.pane(keep).frames_rendered() >= 2);
}
