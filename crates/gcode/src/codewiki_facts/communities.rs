//! Stored import communities, read for evidence through the project's visibility scope.

use super::CodewikiFacts;

/// One stored import community; a stale model label already reads as deterministic.
#[derive(Clone, Debug, PartialEq)]
pub struct CommunityFact {
    pub community_id: i32,
    pub label: String,
    pub label_deterministic: String,
    pub label_source: String,
    pub label_confidence: Option<f64>,
    pub label_stale: bool,
    pub size: usize,
    pub cohesion: f64,
    pub internal_edges: usize,
    pub member_signature: String,
    pub members: Vec<String>,
    pub representatives: Vec<String>,
    pub boundary: Vec<(i32, usize)>,
}

/// `refreshed` tells a persisted empty partition apart from one never computed.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ProjectCommunities {
    pub refreshed: bool,
    pub communities: Vec<CommunityFact>,
}

impl CodewikiFacts {
    pub fn project_communities(&self) -> anyhow::Result<ProjectCommunities> {
        let mut conn = self.read_connection()?;
        let rows = crate::communities::read_for_context(&mut conn, self.context())?;
        let refreshed = crate::communities::partition_refreshed(&mut conn, self.context())?;
        Ok(ProjectCommunities {
            refreshed,
            communities: rows
                .into_iter()
                .map(|row| CommunityFact {
                    community_id: row.community_id,
                    label: row.label,
                    label_deterministic: row.label_deterministic,
                    label_source: row.label_source.as_str().to_string(),
                    label_confidence: row.label_confidence,
                    label_stale: row.label_stale,
                    size: row.member_count,
                    cohesion: row.cohesion,
                    internal_edges: row.internal_edges,
                    member_signature: row.member_signature,
                    members: row.members,
                    representatives: row.representatives,
                    boundary: row.boundary,
                })
                .collect(),
        })
    }
}
