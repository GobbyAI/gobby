use anyhow::{Context as _, anyhow};
use postgres::{Client, GenericClient, Row, Transaction};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::communities::{LabelSource, StoredCommunity};

use super::id_param;

#[derive(Deserialize, Serialize)]
struct BoundaryRow {
    other_community_id: i32,
    import_count: usize,
}

#[allow(dead_code, reason = "consumed by the stored-community read surfaces")]
pub fn read_project_communities(
    conn: &mut impl GenericClient,
    machine_id: &str,
    project_id: &str,
) -> anyhow::Result<Vec<StoredCommunity>> {
    let machine_id = id_param(machine_id)?;
    let project_id = id_param(project_id)?;
    read_rows(conn, &machine_id, &project_id, false)
}

fn read_rows(
    conn: &mut impl GenericClient,
    machine_id: &Uuid,
    project_id: &Uuid,
    for_update: bool,
) -> anyhow::Result<Vec<StoredCommunity>> {
    let suffix = if for_update { " FOR UPDATE" } else { "" };
    let sql = format!(
        "SELECT machine_id::text, project_id::text, community_id, member_count,
                members, representatives, internal_edges, cohesion, boundary::text,
                member_signature, label_deterministic, label, label_source,
                label_confidence, label_model, label_candidates, labeled_signature,
                labeled_at, label_attempted_at, refreshed_at
         FROM code_communities
         WHERE machine_id = $1 AND project_id = $2
         ORDER BY community_id{suffix}"
    );
    conn.query(&sql, &[machine_id, project_id])?
        .into_iter()
        .map(|row| stored_community_from_row(&row, 0))
        .collect()
}

fn stored_community_from_row(row: &Row, offset: usize) -> anyhow::Result<StoredCommunity> {
    let label_source_value: String = row.get(offset + 12);
    let label_source = LabelSource::parse(&label_source_value)
        .ok_or_else(|| anyhow!("invalid stored community label source {label_source_value:?}"))?;
    let boundary_json: String = row.get(offset + 8);
    let boundary = serde_json::from_str::<Vec<BoundaryRow>>(&boundary_json)
        .with_context(|| format!("invalid stored community boundary {boundary_json:?}"))?
        .into_iter()
        .map(|edge| (edge.other_community_id, edge.import_count))
        .collect();
    let member_count: i32 = row.get(offset + 3);
    let internal_edges: i32 = row.get(offset + 6);
    Ok(StoredCommunity {
        machine_id: row.get(offset),
        project_id: row.get(offset + 1),
        community_id: row.get(offset + 2),
        member_count: usize::try_from(member_count)
            .context("stored community member_count is negative")?,
        members: row.get(offset + 4),
        representatives: row.get(offset + 5),
        internal_edges: usize::try_from(internal_edges)
            .context("stored community internal_edges is negative")?,
        cohesion: row.get(offset + 7),
        boundary,
        member_signature: row.get(offset + 9),
        label_deterministic: row.get(offset + 10),
        label: row.get(offset + 11),
        label_source,
        label_confidence: row.get(offset + 13),
        label_model: row.get(offset + 14),
        label_candidates: row.get(offset + 15),
        labeled_signature: row.get(offset + 16),
        labeled_at: row.get(offset + 17),
        label_attempted_at: row.get(offset + 18),
        refreshed_at: row.get(offset + 19),
        label_stale: false,
    })
}

#[allow(dead_code, reason = "consumed by the stored-community read surfaces")]
pub fn read_partition_signature(
    conn: &mut impl GenericClient,
    machine_id: &str,
    project_id: &str,
) -> anyhow::Result<Option<String>> {
    let machine_id = id_param(machine_id)?;
    let project_id = id_param(project_id)?;
    Ok(conn
        .query_opt(
            "SELECT partition_signature
             FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_id, &project_id],
        )?
        .and_then(|row| row.get(0)))
}

pub struct ReplaceTxn<'a> {
    tx: Transaction<'a>,
    machine_id: Uuid,
    project_id: Uuid,
    prior: Vec<StoredCommunity>,
    watermark: i32,
    partition_signature: Option<String>,
}

pub fn begin_replace<'a>(
    conn: &'a mut Client,
    machine_id: &str,
    project_id: &str,
) -> anyhow::Result<ReplaceTxn<'a>> {
    let machine_id = id_param(machine_id)?;
    let project_id = id_param(project_id)?;
    let mut tx = conn.transaction()?;
    let state = tx
        .query_opt(
            "SELECT community_id_watermark, partition_signature
             FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2
             FOR UPDATE",
            &[&machine_id, &project_id],
        )?
        .ok_or_else(|| anyhow!("indexed project state is missing for community replacement"))?;
    let watermark = state.get(0);
    let partition_signature = state.get(1);
    let prior = read_rows(&mut tx, &machine_id, &project_id, true)?;
    Ok(ReplaceTxn {
        tx,
        machine_id,
        project_id,
        prior,
        watermark,
        partition_signature,
    })
}

impl ReplaceTxn<'_> {
    pub fn prior(&self) -> &[StoredCommunity] {
        &self.prior
    }

    pub fn watermark(&self) -> i32 {
        self.watermark
    }

    pub fn partition_signature(&self) -> Option<&str> {
        self.partition_signature.as_deref()
    }

    pub fn seed_from_parent(&mut self, parent_project_id: &str) -> anyhow::Result<()> {
        if !self.prior.is_empty() || self.partition_signature.is_some() {
            return Ok(());
        }
        let parent_project_id = id_param(parent_project_id)?;
        let rows = self.tx.query(
            "SELECT s.community_id_watermark,
                    c.machine_id::text, c.project_id::text, c.community_id, c.member_count,
                    c.members, c.representatives, c.internal_edges, c.cohesion,
                    c.boundary::text, c.member_signature, c.label_deterministic, c.label,
                    c.label_source, c.label_confidence, c.label_model, c.label_candidates,
                    c.labeled_signature, c.labeled_at, c.label_attempted_at, c.refreshed_at
             FROM code_indexed_project_states AS s
             LEFT JOIN code_communities AS c
               ON c.machine_id = s.machine_id AND c.project_id = s.project_id
             WHERE s.machine_id = $1 AND s.project_id = $2
             ORDER BY c.community_id",
            &[&self.machine_id, &parent_project_id],
        )?;
        let state = rows
            .first()
            .ok_or_else(|| anyhow!("parent indexed project state is missing for overlay seed"))?;
        let parent_watermark: i32 = state.get(0);
        self.prior = rows
            .iter()
            .filter(|row| row.get::<_, Option<i32>>(3).is_some())
            .map(|row| stored_community_from_row(row, 1))
            .collect::<anyhow::Result<_>>()?;
        self.watermark = self.watermark.max(parent_watermark);
        Ok(())
    }

    pub fn commit(
        mut self,
        rows: Vec<StoredCommunity>,
        watermark: i32,
        partition_signature: &str,
    ) -> anyhow::Result<()> {
        self.tx.execute(
            "DELETE FROM code_communities WHERE machine_id = $1 AND project_id = $2",
            &[&self.machine_id, &self.project_id],
        )?;
        for row in rows {
            let member_count = i32::try_from(row.member_count)
                .context("community member_count exceeds PostgreSQL integer")?;
            let internal_edges = i32::try_from(row.internal_edges)
                .context("community internal_edges exceeds PostgreSQL integer")?;
            let boundary = serde_json::to_string(
                &row.boundary
                    .into_iter()
                    .map(|(other_community_id, import_count)| BoundaryRow {
                        other_community_id,
                        import_count,
                    })
                    .collect::<Vec<_>>(),
            )?;
            self.tx.execute(
                "INSERT INTO code_communities (
                    machine_id, project_id, community_id, member_count, members,
                    representatives, internal_edges, cohesion, boundary, member_signature,
                    label_deterministic, label, label_source, label_confidence, label_model,
                    label_candidates, labeled_signature, labeled_at, label_attempted_at,
                    refreshed_at
                 ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9::text::jsonb, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18, $19, $20
                 )",
                &[
                    &self.machine_id,
                    &self.project_id,
                    &row.community_id,
                    &member_count,
                    &row.members,
                    &row.representatives,
                    &internal_edges,
                    &row.cohesion,
                    &boundary,
                    &row.member_signature,
                    &row.label_deterministic,
                    &row.label,
                    &row.label_source.as_str(),
                    &row.label_confidence,
                    &row.label_model,
                    &row.label_candidates,
                    &row.labeled_signature,
                    &row.labeled_at,
                    &row.label_attempted_at,
                    &row.refreshed_at,
                ],
            )?;
        }
        self.tx.execute(
            "UPDATE code_indexed_project_states
             SET community_id_watermark = $3, partition_signature = $4
             WHERE machine_id = $1 AND project_id = $2",
            &[
                &self.machine_id,
                &self.project_id,
                &watermark,
                &partition_signature,
            ],
        )?;
        self.tx.commit()?;
        Ok(())
    }

    pub fn skip(self) -> anyhow::Result<()> {
        self.tx.rollback()?;
        Ok(())
    }
}
