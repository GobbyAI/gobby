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
        rows_affected += insert_call_batch(conn, &project_id, content_hash, chunk)?;
    }
    Ok(rows_affected)
}

pub(super) fn insert_call(
    conn: &mut impl GenericClient,
    project_id: &str,
    content_hash: &str,
    call: &CallRelation,
) -> anyhow::Result<usize> {
    let project_id = id_param(project_id)?;
    insert_call_batch(conn, &project_id, content_hash, std::slice::from_ref(call))
}

fn insert_call_batch(
    conn: &mut impl GenericClient,
    project_id: &uuid::Uuid,
    content_hash: &str,
    calls: &[CallRelation],
) -> anyhow::Result<usize> {
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
    let rows = conn.execute(
        "INSERT INTO code_calls
         (project_id, caller_symbol_id, callee_symbol_id, callee_name,
          callee_target_kind, callee_external_module, file_path, content_hash, line)
         SELECT $1, caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, $2, line
         FROM unnest(
            $3::uuid[], $4::uuid[], $5::text[], $6::text[],
            $7::text[], $8::text[], $9::int4[]
         ) AS calls(
            caller_symbol_id, callee_symbol_id, callee_name,
            callee_target_kind, callee_external_module, file_path, line
         )
         ON CONFLICT (
            project_id, file_path, content_hash, caller_symbol_id, callee_symbol_id,
            callee_name, callee_target_kind, callee_external_module, line
         ) DO NOTHING",
        &[
            &project_id,
            &content_hash,
            &caller_symbol_ids,
            &callee_symbol_ids,
            &callee_names,
            &callee_target_kinds,
            &callee_external_modules,
            &file_paths,
            &lines,
        ],
    )?;
    Ok(rows as usize)
}
