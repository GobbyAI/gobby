//! Checkout registration: the hub learns each project root this node serves before it routes work there.
use crate::{node_commands::CONTRACT_VERSION, nodes::hooks::drain, state::NodeServices};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

const CHECKOUTS: &str = "/api/nodes/checkouts";
const PROJECT_MARKER: &str = ".gobby/project.json";
const REGISTER_DEADLINE: Duration = Duration::from_secs(10);

/// Registers the checkout that owns a hook's `cwd`. The hook never waits for the registration.
pub fn observe_hook_cwd(node: Arc<NodeServices>, cwd: PathBuf) {
    tokio::spawn(async move {
        if let Err(error) = register(&node, &cwd).await {
            eprintln!("warning: checkout registration: {error:#}");
        }
    });
}

/// Posts a registration for an unregistered checkout. Only a 2xx answer remembers the root, so a
/// failed registration is retried on the next hook from it.
async fn register(node: &NodeServices, cwd: &Path) -> Result<()> {
    let agents = &node.agents;
    let (home, cwd) = (agents.home.clone(), cwd.to_owned());
    let Some((root, project_id)) =
        tokio::task::spawn_blocking(move || checkout_at(&home, &cwd)).await?
    else {
        return Ok(());
    };
    if agents.registered.lock().await.contains(&root) {
        return Ok(());
    }
    let payload = json!({"v": CONTRACT_VERSION, "project_id": project_id, "root_path": &root});
    let request = drain::request(node, CHECKOUTS, &payload, None).await?;
    let response = tokio::time::timeout(REGISTER_DEADLINE, node.relay.forward(request))
        .await
        .context("checkout registration timed out")?;
    if !response.status().is_success() {
        bail!("checkout registration hub returned {}", response.status());
    }
    agents.registered.lock().await.insert(root);
    Ok(())
}

/// The checkout that owns `cwd`: its root and the project id its marker names. Node workspaces
/// under the gobby home carry a copied marker but are not checkouts.
fn checkout_at(home: &Path, cwd: &Path) -> Option<(PathBuf, String)> {
    let cwd = fs::canonicalize(cwd).ok()?;
    let root = cwd
        .ancestors()
        .find(|dir| dir.join(PROJECT_MARKER).is_file())?;
    if ["worktrees", "clones"]
        .iter()
        .any(|kind| fs::canonicalize(home.join(kind)).is_ok_and(|dir| root.starts_with(dir)))
    {
        return None;
    }
    let bytes = fs::read(root.join(PROJECT_MARKER)).ok()?;
    let id = serde_json::from_slice::<Value>(&bytes).ok()?["id"]
        .as_str()
        .filter(|id| !id.is_empty())?
        .to_owned();
    Some((root.to_owned(), id))
}
