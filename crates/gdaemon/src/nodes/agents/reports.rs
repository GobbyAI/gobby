//! Node reports: the hub's view of this node's accepted runs, live agents and exits.
use super::{
    launch::now_ms,
    state::{ExitRecord, RunPhase, WorkspaceRecord},
    workspace,
};
use crate::{
    heartbeat::HeartbeatHost,
    node_commands::{CONTRACT_VERSION, ReportAgent, ReportBody},
    nodes::hooks::drain,
    state::NodeServices,
};
use anyhow::{Context, Result, bail};
use axum::{body::to_bytes, http::StatusCode};
use serde::Deserialize;
use std::{
    collections::{BTreeSet, HashMap},
    sync::Arc,
    time::Duration,
};

const REPORT: &str = "/api/nodes/report";
const MAX_PAGE: usize = 1000;
const REPORT_DEADLINE: Duration = Duration::from_secs(10);
const JOB_INTERVAL: Duration = Duration::from_secs(30);

#[derive(Deserialize)]
struct Conflict {
    code: String,
    hub_epoch: Option<u64>,
}

/// Posts one report. `report_lock` serializes callers, so a report never overtakes an older one.
pub async fn report_once(node: &NodeServices) -> Result<()> {
    let agents = &node.agents;
    let _turn = agents.report_lock.lock().await;
    // The host is read before the state lock: its call may observe a new host epoch.
    let inventory = agents
        .host
        .call(|client| Box::pin(async move { client.list().await }))
        .await?;
    let live: HashMap<&str, (&str, Option<u64>)> = inventory
        .terminals
        .iter()
        .filter_map(|terminal| {
            Some((
                terminal["terminal_id"].as_str()?,
                (
                    terminal["host_terminal_id"].as_str()?,
                    terminal["pgid"].as_u64(),
                ),
            ))
        })
        .collect();
    let (body, page) = {
        let mut state = agents.state.lock().await;
        let host_epoch_id = state
            .host_epoch_id
            .clone()
            .context("node report waits for the host epoch")?;
        let revision = state.take_report_revision(&agents.home)?;
        let accepted = state
            .runs
            .iter()
            .filter(|(_, run)| matches!(run.phase, RunPhase::Accepted | RunPhase::Preparing))
            .map(|(run_id, _)| run_id.clone())
            .collect();
        let mut reported = Vec::new();
        for (run_id, run) in state
            .runs
            .iter()
            .filter(|(_, run)| run.phase == RunPhase::Launched)
        {
            let Some(host_terminal_id) = run.host_terminal_id.as_deref() else {
                continue;
            };
            let Some((live_host, Some(pid))) = live.get(run.terminal_id.as_str()).copied() else {
                continue;
            };
            if live_host != host_terminal_id {
                continue;
            }
            reported.push(ReportAgent {
                run_id: run_id.clone(),
                terminal_id: run.terminal_id.clone(),
                pid,
                locator: host_terminal_id.to_owned(),
                locator_key: format!("native:{host_epoch_id}:{host_terminal_id}"),
                workspace: run.workspace.as_ref().map(WorkspaceRecord::report),
                pane: None,
                sandbox: None,
            });
        }
        let mut page: Vec<&ExitRecord> = state.exits.iter().collect();
        page.sort_by_key(|record| record.ended_at_ms);
        page.truncate(MAX_PAGE);
        let page: Vec<ExitRecord> = page.into_iter().cloned().collect();
        let body = ReportBody {
            v: CONTRACT_VERSION,
            hub_epoch: state.hub_epoch,
            revision,
            host_epoch: state.host_epoch,
            accepted,
            agents: reported,
            exits: page.iter().map(|record| record.exit.clone()).collect(),
        };
        (body, page)
    };
    let acked: BTreeSet<(String, u64)> = page
        .iter()
        .map(|record| (record.exit.run_id.clone(), record.ended_at_ms))
        .collect();
    let payload = serde_json::to_value(&body)?;
    let request = drain::request(node, REPORT, &payload, None).await?;
    let response = tokio::time::timeout(REPORT_DEADLINE, node.relay.forward(request))
        .await
        .context("node report timed out")?;
    let status = response.status();
    // Only 200 acknowledges the exits and workspaces; other 2xx answers keep them for the next report.
    if status == StatusCode::OK {
        let mut state = agents.state.lock().await;
        for record in &page {
            state.acknowledge_exit(&record.exit.run_id);
        }
        state
            .exits
            .retain(|record| !acked.contains(&(record.exit.run_id.clone(), record.ended_at_ms)));
        // Quiet: an acknowledgement must not start another report.
        state.persist(&agents.home)?;
        return Ok(());
    }
    if status == StatusCode::CONFLICT {
        let bytes = to_bytes(response.into_body(), 64 * 1024).await?;
        let conflict: Conflict =
            serde_json::from_slice(&bytes).context("node report conflict body")?;
        if conflict.code == "stale_report" {
            let hub_epoch = conflict
                .hub_epoch
                .context("stale node report without a hub epoch")?;
            let mut state = agents.state.lock().await;
            state.hub_epoch = Some(hub_epoch);
            return state.commit(&agents.home, &agents.changed);
        }
    }
    bail!("report hub returned {status}")
}

/// Reports once at start, then once per wake: a state commit, the 30-second job, or a stale
/// epoch adopted from the hub.
pub async fn report_loop(node: Arc<NodeServices>) {
    node.agents.changed.notify_one();
    loop {
        node.agents.changed.notified().await;
        if let Err(error) = report_once(&node).await {
            eprintln!("warning: node report: {error:#}");
        }
    }
}

/// Retries queued workspace removals and prunes cancellation records, then wakes the report loop.
pub fn register_jobs(host: &mut HeartbeatHost, node: Arc<NodeServices>) -> Result<()> {
    host.register("node-agent-report", JOB_INTERVAL, move || {
        let agents = node.agents.clone();
        async move {
            workspace::retry_removals(&agents).await?;
            {
                let mut state = agents.state.lock().await;
                state.prune_cancelled(now_ms());
                state.commit(&agents.home, &agents.changed)?;
            }
            agents.changed.notify_one();
            Ok(())
        }
    })
}
