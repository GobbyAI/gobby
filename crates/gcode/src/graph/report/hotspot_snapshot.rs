//! Stored whole-project hotspots for `gcode graph report`.
//!
//! The four hotspot stages (file, symbol, module, incoming calls) each cost more
//! than the 29 s per-query budget on a large overlay, so they are computed on the
//! index path and stored as one snapshot node per overlay scope. A report reads the
//! node when its admitted-version digest and top-n match. On a miss, a report with
//! index write authority computes the stages once and saves the snapshot for the
//! next report; without that authority it computes the same stages live.
//!
//! Writers invalidate every snapshot whose namespaces include the written project,
//! so a stale digest is never served.

use std::collections::{BTreeSet, HashMap};

use anyhow::Context as _;
use gobby_core::degradation::ServiceState;
use gobby_core::falkor::{GraphClient, Row, with_graph};
use sha2::{Digest, Sha256};

use crate::config::Context;
use crate::graph::typed_query::{TypedQuery, TypedValue, cypher_string_literal, string_params};
use crate::index::indexer::IndexDegradation;
use crate::visibility::graph_versions::{
    AdmittedNamespace, admitted_namespaces, has_pending_pinned_graph_projection,
};

use super::DEFAULT_TOP_LIMIT;
use super::loading::load_stored_hotspot_inputs;
use super::rows::{row_string, row_usize};
use super::types::GraphReportHotspots;

/// Bump when the payload encoding or the digest preimage changes.
pub(crate) const SNAPSHOT_FORMAT_VERSION: u32 = 1;

const SNAPSHOT_LABEL: &str = "CodeHotspotSnapshot";
const DIGEST_DOMAIN: &[u8] = b"gcode-hotspot-snapshot-digest\0";

/// One stored snapshot node, read back from the graph.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct StoredSnapshot {
    pub(crate) format: u32,
    pub(crate) top_n: usize,
    pub(crate) digest: String,
    pub(crate) payload: String,
}

/// SHA-256 over the canonical encoding of every admitted namespace: project id,
/// then sorted keys, paths and endpoints, each length-prefixed. The overlay and
/// each source namespace stay separate, so a key that moves between namespaces
/// changes the digest. `pending` is `has_pending_pinned_graph_projection`.
pub(crate) fn admitted_digest(
    namespaces: &[AdmittedNamespace],
    pending: bool,
    format_version: u32,
) -> String {
    let mut digest = Sha256::new();
    digest.update(DIGEST_DOMAIN);
    digest.update(format_version.to_be_bytes());
    digest.update([u8::from(pending)]);
    digest.update((namespaces.len() as u64).to_be_bytes());
    for namespace in namespaces {
        update_field(&mut digest, namespace.project_id.as_bytes());
        update_sorted(&mut digest, &namespace.versions.keys);
        update_sorted(&mut digest, &namespace.versions.paths);
        update_sorted(&mut digest, &namespace.versions.endpoints);
    }
    digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn update_field(digest: &mut Sha256, bytes: &[u8]) {
    digest.update((bytes.len() as u64).to_be_bytes());
    digest.update(bytes);
}

fn update_sorted(digest: &mut Sha256, values: &[String]) {
    let sorted: BTreeSet<&str> = values.iter().map(String::as_str).collect();
    digest.update((sorted.len() as u64).to_be_bytes());
    for value in sorted {
        update_field(digest, value.as_bytes());
    }
}

/// The stored hotspots when the snapshot matches the current digest, format and top-n.
/// A request for more rows than the snapshot stores is served live.
pub(crate) fn stored_hotspots_for(
    stored: Option<&StoredSnapshot>,
    digest: &str,
    top_n: usize,
) -> Option<GraphReportHotspots> {
    let stored = stored?;
    if stored.format != SNAPSHOT_FORMAT_VERSION || stored.digest != digest || top_n > stored.top_n {
        return None;
    }
    let mut hotspots = decode_payload(&stored.payload)?;
    truncate_hotspots(&mut hotspots, top_n);
    Some(hotspots)
}

fn truncate_hotspots(hotspots: &mut GraphReportHotspots, top_n: usize) {
    hotspots.high_degree_files.truncate(top_n);
    hotspots.high_degree_symbols.truncate(top_n);
    hotspots.high_degree_modules.truncate(top_n);
    hotspots.incoming_call_hotspots.truncate(top_n);
}

/// Encodes merged, top-n-truncated hotspots as the snapshot payload.
pub(crate) fn encode_payload(hotspots: &GraphReportHotspots) -> String {
    serde_json::to_string(hotspots).expect("hotspot lists always serialize to JSON")
}

/// Decodes a payload; `None` when it does not parse.
pub(crate) fn decode_payload(payload: &str) -> Option<GraphReportHotspots> {
    serde_json::from_str(payload).ok()
}

/// Cypher that deletes every snapshot node naming `project_id` in its namespaces.
/// The parameter value is raw; `invalidate_snapshots` renders it as a Cypher literal.
pub(crate) fn invalidate_snapshots_query(project_id: &str) -> (String, HashMap<String, String>) {
    (
        format!("MATCH (s:{SNAPSHOT_LABEL}) WHERE $project IN s.namespaces DELETE s"),
        HashMap::from([("project".to_string(), project_id.to_string())]),
    )
}

/// Runs the invalidation after a writer, whether the writer succeeded or failed:
/// a failed write may have left partial graph facts behind. The writer's error wins;
/// otherwise an invalidation error is returned.
pub(crate) fn invalidate_after_write<T>(
    project_id: &str,
    result: anyhow::Result<T>,
    invalidate: impl FnOnce(&str) -> anyhow::Result<()>,
) -> anyhow::Result<T> {
    let invalidation = invalidate(project_id);
    match result {
        Err(error) => {
            if let Err(invalidation_error) = invalidation {
                log::warn!(
                    "hotspot snapshot invalidation for {project_id} failed after a failed write: {invalidation_error:#}"
                );
            }
            Err(error)
        }
        Ok(value) => {
            invalidation?;
            Ok(value)
        }
    }
}

/// The visible degradation for a failed snapshot refresh. The index stays successful.
pub(crate) fn refresh_failure_degradation(result: anyhow::Result<()>) -> Option<IndexDegradation> {
    match result {
        Ok(()) => None,
        Err(error) => Some(IndexDegradation::HotspotSnapshotFailed {
            message: format!("{error:#}"),
        }),
    }
}

/// Clears every snapshot naming `project_id` in its namespaces.
pub(crate) fn invalidate_snapshots(
    client: &mut GraphClient,
    project_id: &str,
) -> anyhow::Result<()> {
    let (query, params) = invalidate_snapshots_query(project_id);
    let rendered: HashMap<String, String> = params
        .into_iter()
        .map(|(name, value)| (name, cypher_string_literal(&value)))
        .collect();
    client.query(&query, Some(rendered))?;
    Ok(())
}

/// The stored hotspots for a report, or `None` when no snapshot serves this digest.
/// A failed read computes live instead of failing the report.
pub(crate) fn read_stored_hotspots(
    client: &mut GraphClient,
    snapshot_project: &str,
    digest: &str,
    top_n: usize,
) -> Option<GraphReportHotspots> {
    match read_snapshot(client, snapshot_project) {
        Ok(stored) => stored_hotspots_for(stored.as_ref(), digest, top_n),
        Err(error) => {
            log::warn!("hotspot snapshot read failed; computing hotspots live: {error:#}");
            None
        }
    }
}

/// The hotspots a report serves after a snapshot miss, saved for the next report.
///
/// Returns `None` when the report must compute live and save nothing: the caller
/// lacks write authority, or `top_n` asks for more rows than a snapshot stores.
/// Otherwise the stages run once at `DEFAULT_TOP_LIMIT`, the full result is saved,
/// and a copy truncated to `top_n` is served. A failed save only logs, symmetric
/// with a failed snapshot read. Like `refresh_after_index`, the save is not fenced
/// against a graph write that lands mid-compute; that writer's invalidation runs
/// first, so its change shows up after the next write or index.
pub(crate) fn hotspots_on_miss<T>(
    state: &mut T,
    top_n: usize,
    may_save: bool,
    compute: impl FnOnce(&mut T, usize) -> anyhow::Result<GraphReportHotspots>,
    save: impl FnOnce(&mut T, &GraphReportHotspots) -> anyhow::Result<()>,
) -> anyhow::Result<Option<GraphReportHotspots>> {
    if !may_save || top_n > DEFAULT_TOP_LIMIT {
        return Ok(None);
    }
    let mut hotspots = compute(state, DEFAULT_TOP_LIMIT)?;
    if let Err(error) = save(state, &hotspots) {
        log::warn!("hotspot snapshot save after a report miss failed: {error:#}");
    }
    truncate_hotspots(&mut hotspots, top_n);
    Ok(Some(hotspots))
}

/// Report-path save-on-miss against the live graph: write authority comes from the
/// context, and a failed authority check computes live like a missing grant.
pub(crate) fn report_hotspots_on_miss(
    client: &mut GraphClient,
    ctx: &Context,
    namespaces: &[AdmittedNamespace],
    digest: &str,
    top_n: usize,
) -> anyhow::Result<Option<GraphReportHotspots>> {
    let may_save = ctx.can_refresh_index().unwrap_or_else(|error| {
        log::warn!("hotspot snapshot write authority check failed; computing live: {error:#}");
        false
    });
    hotspots_on_miss(
        client,
        top_n,
        may_save,
        |client, limit| load_stored_hotspot_inputs(client, namespaces, limit),
        |client, hotspots| write_snapshot(client, &ctx.project_id, namespaces, digest, hotspots),
    )
}

/// Refreshes the stored snapshot after an index run. The compute runs only when the
/// stored digest no longer matches. A failure is a visible degradation, never an
/// index failure.
pub(crate) fn refresh_after_index(ctx: &Context) -> Option<IndexDegradation> {
    let config = ctx.falkordb.as_ref()?;
    let connection_config = config.connection_config();
    let result = match with_graph(Some(&connection_config), &config.graph_name, (), |client| {
        refresh_snapshot(client, ctx)
    }) {
        Ok((_, ServiceState::Available | ServiceState::NotConfigured)) => Ok(()),
        Ok((_, ServiceState::Unreachable { message })) => {
            Err(anyhow::anyhow!("graph unreachable: {message}"))
        }
        Err(error) => Err(error),
    };
    refresh_failure_degradation(result)
}

fn refresh_snapshot(client: &mut GraphClient, ctx: &Context) -> anyhow::Result<()> {
    let namespaces = admitted_namespaces(ctx)?;
    let pending = has_pending_pinned_graph_projection(ctx)?;
    let digest = admitted_digest(&namespaces, pending, SNAPSHOT_FORMAT_VERSION);
    let stored = read_snapshot(client, &ctx.project_id)?;
    if stored_hotspots_for(stored.as_ref(), &digest, DEFAULT_TOP_LIMIT).is_some() {
        return Ok(());
    }
    let hotspots = load_stored_hotspot_inputs(client, &namespaces, DEFAULT_TOP_LIMIT)?;
    write_snapshot(client, &ctx.project_id, &namespaces, &digest, &hotspots)
}

fn read_snapshot(
    client: &mut GraphClient,
    snapshot_project: &str,
) -> anyhow::Result<Option<StoredSnapshot>> {
    let rows = client
        .query(
            &format!(
                "MATCH (s:{SNAPSHOT_LABEL} {{snapshot_project: $snapshot_project}})
                 RETURN s.format AS format, s.top_n AS top_n, s.digest AS digest,
                        s.payload AS payload"
            ),
            Some(string_params(&[("snapshot_project", snapshot_project)])),
        )
        .context("read graph hotspot snapshot")?;
    Ok(rows.first().and_then(row_snapshot))
}

fn row_snapshot(row: &Row) -> Option<StoredSnapshot> {
    Some(StoredSnapshot {
        format: u32::try_from(row_usize(row, &["format"])?).ok()?,
        top_n: row_usize(row, &["top_n"])?,
        digest: row_string(row, &["digest"])?,
        payload: row_string(row, &["payload"])?,
    })
}

fn write_snapshot(
    client: &mut GraphClient,
    snapshot_project: &str,
    namespaces: &[AdmittedNamespace],
    digest: &str,
    hotspots: &GraphReportHotspots,
) -> anyhow::Result<()> {
    let namespace_ids = namespaces
        .iter()
        .map(|namespace| TypedValue::String(namespace.project_id.clone()))
        .collect();
    let query = TypedQuery::with_params(
        format!(
            "MERGE (s:{SNAPSHOT_LABEL} {{snapshot_project: $snapshot_project}})
             SET s.format = $format, s.top_n = $top_n, s.digest = $digest,
                 s.payload = $payload, s.namespaces = $namespaces"
        ),
        [
            (
                "snapshot_project",
                TypedValue::String(snapshot_project.to_string()),
            ),
            (
                "format",
                TypedValue::Integer(i64::from(SNAPSHOT_FORMAT_VERSION)),
            ),
            (
                "top_n",
                TypedValue::Integer(i64::try_from(DEFAULT_TOP_LIMIT)?),
            ),
            ("digest", TypedValue::String(digest.to_string())),
            ("payload", TypedValue::String(encode_payload(hotspots))),
            ("namespaces", TypedValue::List(namespace_ids)),
        ],
    )?;
    let TypedQuery { cypher, params } = query;
    client.query(&cypher, Some(params))?;
    Ok(())
}
