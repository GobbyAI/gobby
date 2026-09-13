//! Machine-local file-state writes fenced to the registered checkout.

use postgres::GenericClient;

use super::IndexWriteMode;
use crate::db::id_param;
use crate::index::checkout_fence;
use crate::models::IndexedFile;

pub fn upsert_file_state(
    conn: &mut impl GenericClient,
    machine_id: &str,
    file: &IndexedFile,
    root_path: &std::path::Path,
    mode: IndexWriteMode,
) -> anyhow::Result<()> {
    let machine_id = id_param(machine_id)?;
    let project_id = id_param(&file.project_id)?;
    let root_path = root_path.to_string_lossy().to_string();
    if mode == IndexWriteMode::Primary {
        let row = conn.query_one(
            "WITH checkout AS (
                SELECT 1
                FROM project_checkouts
                WHERE machine_id = $1 AND project_id = $2 AND root_path = $5
                FOR SHARE
             ), state AS (
                INSERT INTO code_indexed_file_states (
                    machine_id, project_id, file_path, content_hash
                )
                SELECT $1,$2,$3,$4 FROM checkout
                ON CONFLICT(machine_id, project_id, file_path) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    updated_at=NOW()
                RETURNING project_id, file_path, content_hash
             ), referenced AS (
                UPDATE code_indexed_files f
                   SET last_referenced_at = NOW()
                  FROM state s
                 WHERE f.project_id = s.project_id
                   AND f.file_path = s.file_path
                   AND f.content_hash = s.content_hash
                RETURNING 1
             )
             SELECT EXISTS(SELECT 1 FROM checkout)",
            &[
                &machine_id,
                &project_id,
                &file.file_path,
                &file.content_hash,
                &root_path,
            ],
        )?;
        if !row.get::<_, bool>(0) {
            return Err(checkout_fence::mismatch_error(
                conn,
                &machine_id,
                &project_id,
                &root_path,
            ));
        }
        return Ok(());
    }
    conn.execute(
        "WITH state AS (
            INSERT INTO code_indexed_file_states (
                machine_id, project_id, file_path, content_hash
            ) VALUES ($1,$2,$3,$4)
            ON CONFLICT(machine_id, project_id, file_path) DO UPDATE SET
                content_hash=excluded.content_hash,
                updated_at=NOW()
            RETURNING project_id, file_path, content_hash
         )
         UPDATE code_indexed_files f
            SET last_referenced_at = NOW()
           FROM state s
          WHERE f.project_id = s.project_id
            AND f.file_path = s.file_path
            AND f.content_hash = s.content_hash",
        &[
            &machine_id,
            &project_id,
            &file.file_path,
            &file.content_hash,
        ],
    )?;
    Ok(())
}

pub fn adopt_file_state(
    conn: &mut impl GenericClient,
    machine_id: &str,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
    root_path: &std::path::Path,
    mode: IndexWriteMode,
) -> anyhow::Result<bool> {
    let machine_id = id_param(machine_id)?;
    let project_id = id_param(project_id)?;
    let root_path = root_path.to_string_lossy().to_string();
    if mode == IndexWriteMode::Primary {
        let row = conn.query_one(
            "WITH checkout AS (
                SELECT 1
                FROM project_checkouts
                WHERE machine_id = $1 AND project_id = $2 AND root_path = $5
                FOR SHARE
             ), adopted AS (
                INSERT INTO code_indexed_file_states (
                    machine_id, project_id, file_path, content_hash
                )
                SELECT $1, f.project_id, f.file_path, f.content_hash
                FROM code_indexed_files f, checkout
                WHERE f.project_id = $2
                  AND f.file_path = $3
                  AND f.content_hash = $4
                  AND f.graph_synced
                  AND f.vectors_synced
                ON CONFLICT(machine_id, project_id, file_path) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    updated_at=NOW()
                RETURNING project_id, file_path, content_hash
             ), referenced AS (
                UPDATE code_indexed_files f
                   SET last_referenced_at = NOW()
                  FROM adopted a
                 WHERE f.project_id = a.project_id
                   AND f.file_path = a.file_path
                   AND f.content_hash = a.content_hash
                RETURNING 1
             )
             SELECT EXISTS(SELECT 1 FROM checkout), EXISTS(SELECT 1 FROM adopted)",
            &[
                &machine_id,
                &project_id,
                &file_path,
                &content_hash,
                &root_path,
            ],
        )?;
        if !row.get::<_, bool>(0) {
            return Err(checkout_fence::mismatch_error(
                conn,
                &machine_id,
                &project_id,
                &root_path,
            ));
        }
        return Ok(row.get(1));
    }
    let adopted = conn.execute(
        "WITH adopted AS (
            INSERT INTO code_indexed_file_states (
                machine_id, project_id, file_path, content_hash
            )
            SELECT $1, project_id, file_path, content_hash
            FROM code_indexed_files
            WHERE project_id = $2
              AND file_path = $3
              AND content_hash = $4
              AND graph_synced
              AND vectors_synced
            ON CONFLICT(machine_id, project_id, file_path) DO UPDATE SET
                content_hash=excluded.content_hash,
                updated_at=NOW()
            RETURNING project_id, file_path, content_hash
         )
         UPDATE code_indexed_files f
            SET last_referenced_at = NOW()
           FROM adopted a
          WHERE f.project_id = a.project_id
            AND f.file_path = a.file_path
            AND f.content_hash = a.content_hash",
        &[&machine_id, &project_id, &file_path, &content_hash],
    )?;
    Ok(adopted > 0)
}

pub fn delete_file_state(
    conn: &mut impl GenericClient,
    machine_id: &str,
    project_id: &str,
    file_path: &str,
    root_path: &std::path::Path,
    mode: IndexWriteMode,
) -> anyhow::Result<bool> {
    let machine_id = id_param(machine_id)?;
    let project_id = id_param(project_id)?;
    let root_path = root_path.to_string_lossy().to_string();
    if mode == IndexWriteMode::Primary {
        let row = conn.query_one(
            "WITH checkout AS (
                SELECT 1
                FROM project_checkouts
                WHERE machine_id = $1 AND project_id = $2 AND root_path = $4
                FOR SHARE
             ), deleted AS (
                DELETE FROM code_indexed_file_states
                WHERE machine_id = $1 AND project_id = $2 AND file_path = $3
                  AND EXISTS (SELECT 1 FROM checkout)
                RETURNING 1
             )
             SELECT EXISTS(SELECT 1 FROM checkout), EXISTS(SELECT 1 FROM deleted)",
            &[&machine_id, &project_id, &file_path, &root_path],
        )?;
        if !row.get::<_, bool>(0) {
            return Err(checkout_fence::mismatch_error(
                conn,
                &machine_id,
                &project_id,
                &root_path,
            ));
        }
        return Ok(row.get(1));
    }
    let deleted = conn.execute(
        "DELETE FROM code_indexed_file_states
         WHERE machine_id = $1 AND project_id = $2 AND file_path = $3",
        &[&machine_id, &project_id, &file_path],
    )?;
    Ok(deleted > 0)
}
