use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupRequest {
    pub project_id: String,
    pub project_root: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupStatus {
    pub project_id: String,
    pub attached_schema_validated: bool,
}

pub fn setup_status(request: StandaloneSetupRequest) -> StandaloneSetupStatus {
    StandaloneSetupStatus {
        project_id: request.project_id,
        attached_schema_validated: false,
    }
}
