//! Hub-side read entry points. A hub read answers from the database for the verified
//! principal and never opens a path on the hub's own disk.

use std::path::PathBuf;
use std::time::Duration;

use crate::cli_error::CliError;
use crate::commands::search::content_page::{ContentQuery, content_candidates};
use crate::commands::status::current::collect;
use crate::config::{Context, ProjectIndexScope, parent_code_index_exists};
use crate::db;
use crate::index::checkout_fence;
use crate::index::indexer::primary::{self, RunScope};
use crate::index_lock::{self, IndexLockPolicy, IndexLockResult};
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
pub fn plan_index(
    scope: &HubScope,
    request: IndexPlanRequest,
) -> Result<IndexPlanResponse, RouteError> {
    let ctx = scope.context(&request.scope)?;
    let mut conn = connect(&ctx)?;
    let plan = primary::plan_primary(
        &mut conn,
        &ctx,
        RunScope::from_path_filter(request.path_filter.as_deref()),
        request.full,
        &request.files,
    )
    .map_err(|error| route_error("read_failed", error))?;
    Ok(IndexPlanResponse {
        parse: plan.parse,
        parent_indexed: None,
    })
}

/// Upper bound on waiting for the project lock before an apply reports `index_busy`.
const APPLY_LOCK_WAIT: Duration = Duration::from_secs(60);
/// Poll interval while an apply waits for the project lock.
const APPLY_LOCK_POLL: Duration = Duration::from_millis(250);

/// Applies one request's parsed facts, and on the final batch the post-pass, under the
/// project lock. The checkout fence runs before the lock wait so a foreign root fails fast.
pub fn apply_index(
    scope: &HubScope,
    request: IndexApplyRequest,
) -> Result<IndexApplyResponse, RouteError> {
    let ctx = scope.context(&request.scope)?;
    let mut conn = db::connect_readwrite(&ctx.database_url)
        .map_err(|error| route_error("database_unavailable", error))?;
    let machine_id =
        db::id_param(&ctx.machine_id).map_err(|error| route_error("write_failed", error))?;
    let project_id =
        db::id_param(&ctx.project_id).map_err(|error| route_error("write_failed", error))?;
    let root_path = ctx.project_root.to_string_lossy().to_string();
    checkout_fence::require_registered_checkout(&mut conn, &machine_id, &project_id, &root_path)
        .map_err(fence_error)?;
    let run = RunScope::from_path_filter(request.path_filter.as_deref());
    let locked = index_lock::with_project_lock(
        &ctx,
        IndexLockPolicy::BriefTry {
            total_wait: APPLY_LOCK_WAIT,
            poll: APPLY_LOCK_POLL,
        },
        || {
            primary::apply_primary(
                &mut conn,
                &ctx,
                run,
                request.full,
                &request.files,
                request.final_batch.as_ref(),
            )
        },
    )
    .map_err(|error| route_error("write_failed", error))?;
    match locked {
        IndexLockResult::Acquired(applied) => Ok(IndexApplyResponse {
            reparse: applied.reparse,
            outcome: applied.outcome,
            partial: applied.partial,
        }),
        IndexLockResult::Busy(holder) => Err(RouteError {
            code: "index_busy".to_string(),
            error: "index busy".to_string(),
            message: holder.unwrap_or_else(|| "project index is locked by another run".to_string()),
        }),
    }
}

/// Keeps a checkout refusal's code and message; any other failure is a write failure.
fn fence_error(error: anyhow::Error) -> RouteError {
    match error.downcast::<CliError>() {
        Ok(cli) => RouteError {
            code: cli.code.to_string(),
            error: cli.code.replace('_', " "),
            message: cli.message,
        },
        Err(error) => route_error("write_failed", error),
    }
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
