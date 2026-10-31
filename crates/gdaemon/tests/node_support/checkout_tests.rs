//! #24378: checkout registration is triggered by the real forwarded hook path.

#[path = "fake_hub.rs"]
pub(super) mod fake_hub;
#[path = "node_home.rs"]
pub(super) mod node_home;

use anyhow::Result;
use axum::{
    body::{Body, to_bytes},
    http::{Method, Request, StatusCode},
};
use fake_hub::{FakeHub, RecordedRequest};
use gobby_daemon::{front_door::FrontDoor, node_commands::CONTRACT_VERSION};
use node_home::{NODE_KEY, NodeHome};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

const HOOK: &str = "/api/hooks/execute";
const CHECKOUTS: &str = "/api/nodes/checkouts";
const PROJECT: &str = "763c1ccf-67cf-4f40-a025-b9024accc14b";
const OUTER_PROJECT: &str = "8020a3ca-cb22-47af-b3ee-bc28c5c74e43";

fn project_root(path: &Path, id: &str) -> Result<PathBuf> {
    fs::create_dir_all(path.join(".gobby"))?;
    fs::write(
        path.join(".gobby/project.json"),
        serde_json::to_vec(&json!({"id":id}))?,
    )?;
    Ok(fs::canonicalize(path)?)
}

async fn hook(node: &NodeHome, cwd: &Path, envelope: &str) -> Result<(StatusCode, Value)> {
    let payload = json!({
        "hook_type":"pre_tool_use", "response_capability":"hook-response.v1",
        "input_data":{"cwd":cwd, "tool_name":"Read", "tool_input":{"file_path":"README.md"}},
    });
    let request = Request::builder()
        .method(Method::POST)
        .uri(HOOK)
        .header("authorization", format!("Bearer {NODE_KEY}"))
        .header("content-type", "application/json")
        .header("x-gobby-envelope-id", envelope)
        .body(Body::from(serde_json::to_vec(&payload)?))?;
    let response = tokio::time::timeout(
        Duration::from_secs(3),
        FrontDoor::node(node.node.clone(), false).handle(
            request,
            "127.0.0.1:12345".parse()?,
            false,
        ),
    )
    .await
    .expect("hook forwarding must not wait for checkout registration");
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 1024 * 1024).await?;
    Ok((status, serde_json::from_slice(&bytes)?))
}

fn registrations(hub: &FakeHub) -> Vec<RecordedRequest> {
    hub.requests()
        .into_iter()
        .filter(|r| r.path == CHECKOUTS)
        .collect()
}

async fn wait_for_registrations(hub: &FakeHub, count: usize) -> Vec<RecordedRequest> {
    let observed = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let requests = registrations(hub);
            if requests.len() >= count {
                return requests;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    assert!(
        observed.is_ok(),
        "forwarded hook did not register its checkout; requests={:?}",
        hub.requests()
    );
    observed.unwrap()
}

async fn wait_until_registered(node: &NodeHome, root: &Path) {
    let registered = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if node.node.agents.registered.lock().await.contains(root) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    assert!(
        registered.is_ok(),
        "successful registration was not remembered for {root:?}"
    );
}

async fn assert_registration_count(hub: &FakeHub, expected: usize) {
    let extra = tokio::time::timeout(Duration::from_millis(100), async {
        loop {
            if registrations(hub).len() > expected {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    assert!(
        extra.is_err(),
        "unexpected background registration: {:?}",
        registrations(hub)
    );
    assert_eq!(registrations(hub).len(), expected);
}

fn assert_registration(request: &RecordedRequest, root: &Path, id: &str) {
    assert_eq!(request.method, Method::POST);
    assert_eq!(
        request.headers["authorization"],
        format!("Bearer {NODE_KEY}")
    );
    assert_eq!(
        request.body,
        json!({"v":CONTRACT_VERSION, "project_id":id, "root_path":root})
    );
}

#[tokio::test]
async fn checkout_registered_on_first_hook() -> Result<()> {
    let hub = FakeHub::start().await?;
    let node = NodeHome::new(&hub)?;
    let tree = tempfile::tempdir()?;
    let outer = project_root(tree.path(), OUTER_PROJECT)?;
    let root = project_root(&outer.join("nested-project"), PROJECT)?;
    let cwd = root.join("src/deeper");
    fs::create_dir_all(&cwd)?;
    let refusal = json!({"code":"hook_refused", "message":"fixture refuses hook, not checkout"});
    hub.script(HOOK, vec![(StatusCode::FORBIDDEN, refusal.clone()); 4]);
    hub.script(
        CHECKOUTS,
        vec![(
            StatusCode::CREATED,
            json!({"root_path":root, "created":true}),
        )],
    );
    let (entered, release) = hub.pause_next(CHECKOUTS);

    // The registration is held on the hub. The hook must still return its own refusal.
    let (status, response) = hook(&node, &cwd, "checkout-first").await?;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(response, refusal);
    let attempted = tokio::time::timeout(Duration::from_secs(3), entered).await;
    assert!(
        attempted.is_ok(),
        "first refused hook never attempted checkout registration"
    );
    attempted
        .unwrap()
        .expect("registration barrier was reached");
    let requests = registrations(&hub);
    assert_eq!(requests.len(), 1);
    assert_registration(&requests[0], &root, PROJECT);
    assert!(
        !node.node.agents.registered.lock().await.contains(&root),
        "a request is not cached before a 2xx"
    );
    release.send(()).expect("release registration response");
    wait_until_registered(&node, &root).await;

    for (cwd, envelope) in [
        (&root, "checkout-repeat-root"),
        (&cwd, "checkout-repeat-child"),
        (&root, "checkout-repeat-again"),
    ] {
        assert_eq!(hook(&node, cwd, envelope).await?.0, StatusCode::FORBIDDEN);
    }
    assert_registration_count(&hub, 1).await;
    assert_eq!(
        hub.requests().iter().filter(|r| r.path == HOOK).count(),
        4,
        "all hooks were actually forwarded"
    );
    hub.shutdown().await;
    Ok(())
}

#[tokio::test]
async fn checkout_registration_retries_and_skips_workspaces() -> Result<()> {
    let hub = FakeHub::start().await?;
    let node = NodeHome::new(&hub)?;
    let tree = tempfile::tempdir()?;
    let root = project_root(&tree.path().join("checkout"), PROJECT)?;
    hub.script(HOOK, vec![(StatusCode::OK, json!({"decision":"allow"})); 7]);
    hub.script(
        CHECKOUTS,
        vec![
            (
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"code":"temporary_failure"}),
            ),
            (StatusCode::OK, json!({"root_path":root, "created":false})),
        ],
    );
    assert_eq!(hook(&node, &root, "retry-first").await?.0, StatusCode::OK);
    let first = wait_for_registrations(&hub, 1).await;
    assert_registration(&first[0], &root, PROJECT);
    assert!(
        !node.node.agents.registered.lock().await.contains(&root),
        "failed registration must remain retryable"
    );
    assert_eq!(
        hook(&node, &root, "retry-next-hook").await?.0,
        StatusCode::OK
    );
    let retry = wait_for_registrations(&hub, 2).await;
    assert_registration(&retry[1], &root, PROJECT);
    wait_until_registered(&node, &root).await;
    assert_eq!(
        hook(&node, &root, "retry-after-success").await?.0,
        StatusCode::OK
    );

    for (kind, envelope) in [("worktrees", "skip-worktree"), ("clones", "skip-clone")] {
        let workspace = project_root(
            &node.home.path().join(kind).join(PROJECT).join("run"),
            PROJECT,
        )?;
        let cwd = workspace.join("src");
        fs::create_dir_all(&cwd)?;
        assert_eq!(hook(&node, &cwd, envelope).await?.0, StatusCode::OK);
        assert!(
            !node
                .node
                .agents
                .registered
                .lock()
                .await
                .contains(&workspace)
        );
    }
    let unmarked = tree.path().join("without-project-marker");
    fs::create_dir_all(&unmarked)?;
    assert_eq!(
        hook(&node, &unmarked, "skip-no-marker").await?.0,
        StatusCode::OK
    );
    // A similarly named sibling is an ordinary checkout, not a managed workspace.
    let sibling = project_root(&node.home.path().join("worktrees-sibling"), PROJECT)?;
    hub.script(
        CHECKOUTS,
        vec![(StatusCode::OK, json!({"root_path":sibling, "created":true}))],
    );
    assert_eq!(
        hook(&node, &sibling, "register-prefix-sibling").await?.0,
        StatusCode::OK
    );
    let all = wait_for_registrations(&hub, 3).await;
    wait_until_registered(&node, &sibling).await;
    assert_eq!(all.len(), 3, "workspace or unmarked cwd was registered");
    assert_registration(&all[2], &sibling, PROJECT);
    assert_registration_count(&hub, 3).await;
    assert_eq!(hub.requests().iter().filter(|r| r.path == HOOK).count(), 7);
    hub.shutdown().await;
    Ok(())
}

#[tokio::test]
async fn concurrent_unregistered_hooks_register_once() -> Result<()> {
    let hub = FakeHub::start().await?;
    let node = NodeHome::new(&hub)?;
    let tree = tempfile::tempdir()?;
    let root = project_root(&tree.path().join("checkout"), PROJECT)?;
    let cwd = root.join("src");
    fs::create_dir_all(&cwd)?;
    hub.script(HOOK, vec![(StatusCode::OK, json!({"decision":"allow"})); 2]);
    hub.script(
        CHECKOUTS,
        vec![
            (
                StatusCode::CREATED,
                json!({"root_path":root, "created":true})
            );
            2
        ],
    );
    let (entered, release) = hub.pause_next(CHECKOUTS);
    assert_eq!(
        hook(&node, &root, "concurrent-first").await?.0,
        StatusCode::OK
    );
    let attempted = tokio::time::timeout(Duration::from_secs(3), entered).await;
    assert!(
        attempted.is_ok(),
        "first registration never reached the hub"
    );
    attempted
        .unwrap()
        .expect("registration barrier was reached");
    // The first registration is still held. A second hook from the same root must not POST again.
    assert_eq!(
        hook(&node, &cwd, "concurrent-second").await?.0,
        StatusCode::OK
    );
    assert_registration_count(&hub, 1).await;
    release.send(()).expect("release registration response");
    wait_until_registered(&node, &root).await;
    assert_registration_count(&hub, 1).await;
    hub.shutdown().await;
    Ok(())
}

#[tokio::test]
async fn authenticated_pass_through_hook_registers_checkout() -> Result<()> {
    let hub = FakeHub::start().await?;
    let node = NodeHome::new(&hub)?;
    let tree = tempfile::tempdir()?;
    let root = project_root(&tree.path().join("checkout"), PROJECT)?;
    hub.script(HOOK, vec![(StatusCode::OK, json!({"decision":"allow"}))]);
    hub.script(
        CHECKOUTS,
        vec![(
            StatusCode::CREATED,
            json!({"root_path":root, "created":true}),
        )],
    );
    // No hook-response capability and no envelope ID: the hook takes the pass-through branch.
    let payload = json!({
        "hook_type":"pre_tool_use",
        "input_data":{"cwd":root, "tool_name":"Read", "tool_input":{"file_path":"README.md"}},
    });
    let request = Request::builder()
        .method(Method::POST)
        .uri(HOOK)
        .header("authorization", format!("Bearer {NODE_KEY}"))
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&payload)?))?;
    let response = FrontDoor::node(node.node.clone(), false)
        .handle(request, "127.0.0.1:12345".parse()?, false)
        .await;
    assert_eq!(response.status(), StatusCode::OK);
    let requests = wait_for_registrations(&hub, 1).await;
    assert_registration(&requests[0], &root, PROJECT);
    wait_until_registered(&node, &root).await;
    hub.shutdown().await;
    Ok(())
}
