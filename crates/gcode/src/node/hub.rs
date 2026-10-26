//! Hub-side read entry points. A hub read answers from the database for the verified
//! principal and never opens a path on the hub's own disk.

use std::path::PathBuf;

use crate::commands::search::content_page::{ContentQuery, content_candidates};
use crate::commands::status::current::collect;
use crate::config::{Context, ProjectIndexScope, parent_code_index_exists};
use crate::db;
use crate::node::{
    ContentCandidates, IndexApplyRequest, IndexApplyResponse, IndexPlanRequest, IndexPlanResponse,
    NodeScope, RouteError, SearchContentRequest, StatusReport, StatusRequest,
};

/// The verified principal a hub read runs as.
#[derive(Debug, Clone)]
pub struct HubScope {
    pub database_url: String,
    pub machine_id: String,
    pub project_id: String,
    pub overlay_project_id: Option<String>,
}

impl HubScope {
    /// Builds the read context for a node request, after the overlay claims check.
    pub fn context(&self, scope: &NodeScope) -> Result<Context, RouteError> {
        // A local overlay context carries the overlay's own id; the parent sits in the scope.
        let (project_id, index_scope) = match (&scope.parent_root, &self.overlay_project_id) {
            (None, None) => (self.project_id.clone(), ProjectIndexScope::Single),
            (Some(parent_root), Some(overlay_project_id)) => (
                overlay_project_id.clone(),
                ProjectIndexScope::Overlay {
                    overlay_project_id: overlay_project_id.clone(),
                    overlay_root: PathBuf::from(&scope.root),
                    parent_project_id: self.project_id.clone(),
                    parent_root: PathBuf::from(parent_root),
                },
            ),
            _ => {
                return Err(RouteError {
                    code: "claims_mismatch".to_string(),
                    error: "claims mismatch".to_string(),
                    message: "the overlay shape disagrees with the principal".to_string(),
                });
            }
        };
        Ok(Context::for_machine(
            &self.database_url,
            &self.machine_id,
            &project_id,
            PathBuf::from(&scope.root),
            index_scope,
        ))
    }
}

/// Status of the node's project, read for the principal's machine.
pub fn status(scope: &HubScope, request: StatusRequest) -> Result<StatusReport, RouteError> {
    let ctx = scope.context(&request.scope)?;
    let mut conn = connect(&ctx)?;
    collect(&mut conn, &ctx).map_err(|error| route_error("read_failed", error))
}

/// Content candidates for the node's project, read for the principal's machine.
pub fn search_content(
    scope: &HubScope,
    request: SearchContentRequest,
) -> Result<ContentCandidates, RouteError> {
    let ctx = scope.context(&request.scope)?;
    let mut conn = connect(&ctx)?;
    if let ProjectIndexScope::Overlay {
        parent_project_id, ..
    } = &ctx.index_scope
    {
        let exists = parent_code_index_exists(&mut conn, parent_project_id, &ctx.machine_id)
            .map_err(|error| route_error("read_failed", error))?;
        if !exists {
            return Err(RouteError {
                code: "parent_index_missing".to_string(),
                error: "parent index missing".to_string(),
                message: format!(
                    "no code index exists for parent project {parent_project_id} on this machine"
                ),
            });
        }
    }
    let query = ContentQuery {
        language: request.language.as_deref(),
        paths: &request.paths,
        limit: request.limit,
        offset: request.offset,
    };
    content_candidates(&mut conn, &ctx, &request.query, query)
        .map_err(|error| route_error("read_failed", error))
}

/// Plans a Primary index run on a read-only connection, without the project lock.
/// Stub: the implementation runs `plan_primary` (see 1.4).
pub fn plan_index(
    _scope: &HubScope,
    _request: IndexPlanRequest,
) -> Result<IndexPlanResponse, RouteError> {
    todo!("1.4 hub: plan_primary on a read-only connection")
}

/// Applies one request's parsed facts, and on the final batch the post-pass, under the
/// project lock. Stub: the implementation runs `apply_primary` (see 1.4).
pub fn apply_index(
    _scope: &HubScope,
    _request: IndexApplyRequest,
) -> Result<IndexApplyResponse, RouteError> {
    todo!("1.4 hub: apply_primary under index_lock::with_project_lock")
}

fn connect(ctx: &Context) -> Result<postgres::Client, RouteError> {
    db::connect_readonly(&ctx.database_url)
        .map_err(|error| route_error("database_unavailable", error))
}

fn route_error(code: &str, error: anyhow::Error) -> RouteError {
    RouteError {
        code: code.to_string(),
        error: code.replace('_', " "),
        message: format!("{error:#}"),
    }
}
