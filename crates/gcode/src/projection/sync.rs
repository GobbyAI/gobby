use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectionTarget {
    Graph,
    Vectors,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectionSyncRequest {
    pub project_id: String,
    pub file_paths: Vec<String>,
    pub targets: Vec<ProjectionTarget>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectionSyncStatus {
    pub project_id: String,
    pub file_paths: Vec<String>,
    pub graph_pending: bool,
    pub vectors_pending: bool,
}

pub fn pending_after_code_fact_write(request: ProjectionSyncRequest) -> ProjectionSyncStatus {
    ProjectionSyncStatus {
        graph_pending: request.targets.contains(&ProjectionTarget::Graph),
        vectors_pending: request.targets.contains(&ProjectionTarget::Vectors),
        project_id: request.project_id,
        file_paths: request.file_paths,
    }
}
