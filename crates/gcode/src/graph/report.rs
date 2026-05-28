use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectGraphReport {
    pub project_id: String,
    pub files: usize,
    pub symbols: usize,
    pub imports: usize,
    pub calls: usize,
}

pub fn empty_report(project_id: impl Into<String>) -> ProjectGraphReport {
    ProjectGraphReport {
        project_id: project_id.into(),
        files: 0,
        symbols: 0,
        imports: 0,
        calls: 0,
    }
}
