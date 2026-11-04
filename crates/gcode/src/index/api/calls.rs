use postgres::GenericClient;

use crate::db::{id_param, opt_id_param};
use crate::models::CallRelation;

use super::to_i32;

const CALL_UPSERT_BATCH_SIZE: usize = 500;

pub fn upsert_calls(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
    calls: &[CallRelation],
) -> anyhow::Result<usize> {
    let project_id = id_param(project_id)?;
    conn.execute(
        "DELETE FROM code_calls
         WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
        &[&project_id, &file_path, &content_hash],
    )?;

    let mut rows_affected = 0usize;
    for chunk in calls.chunks(CALL_UPSERT_BATCH_SIZE) {
        let rows = chunk
            .iter()
            .map(|call| (content_hash, call))
            .collect::<Vec<_>>();
        rows_affected += insert_call_batch(conn, &project_id, &rows)?;
    }
    Ok(rows_affected)
}

/// Inserts calls in one statement. Each row carries its own content hash, so
/// rows from different files can share a batch.
pub(super) fn insert_call_batch(
    conn: &mut impl GenericClient,
    project_id: &uuid::Uuid,
    rows: &[(&str, &CallRelation)],
) -> anyhow::Result<usize> {
    let calls = rows.iter().map(|(_, call)| *call).collect::<Vec<_>>();
    let content_hashes = rows
        .iter()
        .map(|(content_hash, _)| (*content_hash).to_string())
        .collect::<Vec<_>>();
    let caller_symbol_ids = calls
        .iter()
        .map(|call| opt_id_param(&call.caller_symbol_id))
        .collect::<anyhow::Result<Vec<_>>>()?;
    let callee_symbol_ids = calls
        .iter()
        .map(|call| opt_id_param(call.callee_symbol_id.as_deref().unwrap_or("")))
        .collect::<anyhow::Result<Vec<_>>>()?;
    let callee_names = calls
        .iter()
        .map(|call| call.callee_name.clone())
        .collect::<Vec<_>>();
    let callee_target_kinds = calls
        .iter()
        .map(|call| call.callee_target_kind.as_str().to_string())
        .collect::<Vec<_>>();
    let callee_external_modules = calls
        .iter()
        .map(|call| call.callee_external_module.clone().unwrap_or_default())
        .collect::<Vec<_>>();
    let file_paths = calls
        .iter()
        .map(|call| call.file_path.clone())
        .collect::<Vec<_>>();
    let lines = calls
        .iter()
        .map(|call| to_i32(call.line))
        .collect::<Vec<_>>();

    // Empty IDs are domain sentinels for nullable UUID targets. The unique
    // constraint uses NULLS NOT DISTINCT, preserving deduplication for them.
    let inserted = conn.execute(
        "INSERT INTO code_calls
         (project_id, caller_symbol_id, callee_symbol_id, callee_name,
          callee_target_kind, callee_external_module, file_path, content_hash, line)
         SELECT $1, caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, content_hash, line
         FROM unnest(
            $2::uuid[], $3::uuid[], $4::text[], $5::text[],
            $6::text[], $7::text[], $8::text[], $9::int4[]
         ) AS calls(
            caller_symbol_id, callee_symbol_id, callee_name,
            callee_target_kind, callee_external_module, file_path, content_hash, line
         )
         ON CONFLICT (
            project_id, file_path, content_hash, caller_symbol_id, callee_symbol_id,
            callee_name, callee_target_kind, callee_external_module, line
         ) DO NOTHING",
        &[
            &project_id,
            &caller_symbol_ids,
            &callee_symbol_ids,
            &callee_names,
            &callee_target_kinds,
            &callee_external_modules,
            &file_paths,
            &content_hashes,
            &lines,
        ],
    )?;
    Ok(inserted as usize)
}
