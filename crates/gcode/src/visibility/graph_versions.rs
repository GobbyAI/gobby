//! Content versions a graph read may serve. Graph edges carry the content hash
//! they were projected from, so reads filter on `path|hash` before any LIMIT or
//! aggregation. Each admitted version is served by one graph namespace: the
//! scope's own namespace for owned files and projected pins, or the source
//! project for a pin whose graph projection has not landed yet.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use crate::config::Context;
use crate::db;
use crate::graph::typed_query;
use crate::models::{SearchWarning, SearchWarningCause, SearchWarningLane};

use super::{
    TOMBSTONE_LANGUAGE, file_selectors_sql, machine_uuid_or_invisible, project_uuid_or_invisible,
    scope_project_id,
};

/// Degraded-state notices for graph reads. A pin whose graph projection has not
/// landed and whose source no longer names its hash serves no graph facts, so
/// reads report it instead of returning empty.
pub(crate) fn pending_projection_warnings(ctx: &Context) -> Vec<SearchWarning> {
    match has_pending_pinned_graph_projection(ctx) {
        Ok(true) => vec![SearchWarning {
            lane: SearchWarningLane::Graph,
            cause: SearchWarningCause::ProjectionPending,
            message: "graph results degraded: pinned graph projection pending recovery; \
                      pinned files are omitted until it lands"
                .to_string(),
        }],
        Ok(false) => Vec::new(),
        Err(error) => {
            log::warn!("pinned graph projection state unavailable: {error}");
            Vec::new()
        }
    }
}

pub(crate) fn has_pending_pinned_graph_projection(ctx: &Context) -> anyhow::Result<bool> {
    let (Some(machine), Some(project)) = (
        machine_uuid_or_invisible(&ctx.machine_id),
        project_uuid_or_invisible(scope_project_id(ctx)),
    ) else {
        return Ok(false);
    };
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let row = conn.query_one(
        "SELECT EXISTS (
             SELECT 1 FROM code_overlay_pins p
             WHERE p.machine_id = $1 AND p.overlay_project_id = $2
               AND NOT p.graph_projected
               AND NOT EXISTS (
                   SELECT 1 FROM code_indexed_file_states shadow
                   WHERE shadow.machine_id = $1 AND shadow.project_id = $2
                     AND shadow.file_path = p.file_path
               )
               AND NOT EXISTS (
                   SELECT 1 FROM code_indexed_file_states src
                   WHERE src.machine_id = p.machine_id
                     AND src.project_id = p.source_project_id
                     AND src.file_path = p.file_path
                     AND src.content_hash = p.content_hash
               )
         )",
        &[&machine, &project],
    )?;
    Ok(row.try_get(0)?)
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(crate) struct AdmittedGraphVersions {
    /// `path|content_hash` for every admitted file version.
    pub(crate) keys: Vec<String>,
    /// Every path with at least one admitted version.
    pub(crate) paths: Vec<String>,
    /// `path|content_hash` for every version admitted by any namespace in the scope.
    /// Edge endpoints that resolve to a callee placeholder check this set.
    pub(crate) endpoints: Vec<String>,
}

/// One graph namespace a read runs in: the project id its nodes carry and the
/// `path|hash` versions it admits there. Node ids and edges are namespace-local,
/// so a read runs once per namespace and never mixes versions across them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct AdmittedNamespace {
    pub(crate) project_id: String,
    pub(crate) versions: AdmittedGraphVersions,
}

/// Namespaces the scope's graph reads run in. The scope's own namespace comes
/// first and is always present, so an empty overlay still answers with empty
/// results. Each source project that serves a fresh pin adds one namespace.
///
/// Owned file states and graph-projected pins are served from the scope's own
/// namespace. An unprojected pin is served from its source project while the
/// source selector still names the pinned hash and no owned state shadows it.
pub(crate) fn admitted_namespaces(ctx: &Context) -> anyhow::Result<Vec<AdmittedNamespace>> {
    let (Some(machine), Some(project)) = (
        machine_uuid_or_invisible(&ctx.machine_id),
        project_uuid_or_invisible(scope_project_id(ctx)),
    ) else {
        return Ok(vec![AdmittedNamespace {
            project_id: ctx.project_id.clone(),
            versions: AdmittedGraphVersions::default(),
        }]);
    };
    let owned = file_selectors_sql("$1", "$2", false);
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let mut overlay = VersionSet::default();
    for row in conn.query(
        &format!(
            "SELECT file_path, content_hash FROM ({owned}) owned
             UNION
             SELECT p.file_path, p.content_hash
             FROM code_overlay_pins p
             JOIN code_indexed_files f
               ON f.project_id = p.source_project_id AND f.file_path = p.file_path
              AND f.content_hash = p.content_hash
             WHERE p.machine_id = $1 AND p.overlay_project_id = $2
               AND p.graph_projected
               AND f.language != '{TOMBSTONE_LANGUAGE}'
               AND NOT EXISTS (
                   SELECT 1 FROM code_indexed_file_states shadow
                   WHERE shadow.machine_id = $1 AND shadow.project_id = $2
                     AND shadow.file_path = p.file_path
               )"
        ),
        &[&machine, &project],
    )? {
        overlay.admit(
            &row.try_get::<_, String>("file_path")?,
            &row.try_get::<_, String>("content_hash")?,
        );
    }

    let mut sources = BTreeMap::<String, VersionSet>::new();
    for row in conn.query(
        &format!(
            "SELECT p.source_project_id::text AS source_project_id, p.file_path, p.content_hash
             FROM code_overlay_pins p
             JOIN code_indexed_files f
               ON f.project_id = p.source_project_id AND f.file_path = p.file_path
              AND f.content_hash = p.content_hash
             WHERE p.machine_id = $1 AND p.overlay_project_id = $2
               AND NOT p.graph_projected
               AND f.language != '{TOMBSTONE_LANGUAGE}'
               AND EXISTS (
                   SELECT 1 FROM code_indexed_file_states src
                   WHERE src.machine_id = p.machine_id
                     AND src.project_id = p.source_project_id
                     AND src.file_path = p.file_path
                     AND src.content_hash = p.content_hash
               )
               AND NOT EXISTS (
                   SELECT 1 FROM code_indexed_file_states shadow
                   WHERE shadow.machine_id = $1 AND shadow.project_id = $2
                     AND shadow.file_path = p.file_path
               )"
        ),
        &[&machine, &project],
    )? {
        sources
            .entry(row.try_get::<_, String>("source_project_id")?)
            .or_default()
            .admit(
                &row.try_get::<_, String>("file_path")?,
                &row.try_get::<_, String>("content_hash")?,
            );
    }

    let mut namespaces = vec![AdmittedNamespace {
        project_id: ctx.project_id.clone(),
        versions: overlay.into_versions(),
    }];
    namespaces.extend(
        sources
            .into_iter()
            .map(|(project_id, set)| AdmittedNamespace {
                project_id,
                versions: set.into_versions(),
            }),
    );
    // Every namespace in the scope shares one endpoint set. A callee placeholder
    // in one namespace can name a version that another namespace serves.
    let endpoints: Vec<String> = namespaces
        .iter()
        .flat_map(|namespace| namespace.versions.keys.iter().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    for namespace in &mut namespaces {
        namespace.versions.endpoints = endpoints.clone();
    }
    Ok(namespaces)
}

#[derive(Default)]
struct VersionSet {
    keys: BTreeSet<String>,
    paths: BTreeSet<String>,
}

impl VersionSet {
    fn admit(&mut self, path: &str, hash: &str) {
        self.keys.insert(format!("{path}|{hash}"));
        self.paths.insert(path.to_string());
    }

    fn into_versions(self) -> AdmittedGraphVersions {
        AdmittedGraphVersions {
            keys: self.keys.into_iter().collect(),
            paths: self.paths.into_iter().collect(),
            endpoints: Vec::new(),
        }
    }
}

/// Cypher list of `{p, h}` maps, one per admitted `path|hash` key. Hashes are
/// hex, so the last `|` separates the path from the hash.
fn admitted_pair_literal(keys: &[String]) -> String {
    keys.iter()
        .filter_map(|key| key.rsplit_once('|'))
        .map(|(path, hash)| {
            format!(
                "{{p: {}, h: {}}}",
                typed_query::cypher_string_literal(path),
                typed_query::cypher_string_literal(hash)
            )
        })
        .collect::<Vec<_>>()
        .join(", ")
}

/// Cypher map from each path to its admitted hashes, so `$map[path]` is a keyed
/// lookup instead of a scan over every `path|hash` key.
fn hash_map_literal(keys: &[String]) -> String {
    let mut by_path = BTreeMap::<&str, Vec<&str>>::new();
    for key in keys {
        if let Some((path, hash)) = key.rsplit_once('|') {
            by_path.entry(path).or_default().push(hash);
        }
    }
    by_path
        .iter()
        .map(|(path, hashes)| {
            format!(
                "`{}`: [{}]",
                path.replace('`', "``"),
                hashes
                    .iter()
                    .map(|hash| typed_query::cypher_string_literal(hash))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        })
        .collect::<Vec<_>>()
        .join(", ")
}

/// Binds `$admitted_versions` and `$admitted_paths` into a query built by a
/// payload or report builder.
pub(crate) fn with_admitted_versions(
    (query, mut params): (String, HashMap<String, String>),
    admitted: &AdmittedGraphVersions,
) -> (String, HashMap<String, String>) {
    params.insert(
        "admitted_versions".to_string(),
        format!("[{}]", typed_query::id_list_literal(&admitted.keys)),
    );
    params.insert(
        "admitted_pairs".to_string(),
        format!("[{}]", admitted_pair_literal(&admitted.keys)),
    );
    params.insert(
        "admitted_map".to_string(),
        format!("{{{}}}", hash_map_literal(&admitted.keys)),
    );
    params.insert(
        "endpoint_map".to_string(),
        format!("{{{}}}", hash_map_literal(&admitted.endpoints)),
    );
    params.insert(
        "admitted_paths".to_string(),
        format!("[{}]", typed_query::id_list_literal(&admitted.paths)),
    );
    params.insert(
        "admitted_endpoints".to_string(),
        format!("[{}]", typed_query::id_list_literal(&admitted.endpoints)),
    );
    (query, params)
}
