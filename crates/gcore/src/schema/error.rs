use thiserror::Error;

use super::gate::BackupManifestError;

#[derive(Debug, Error)]
pub enum SchemaError {
    #[error("invalid PostgreSQL schema name: {0}")]
    InvalidSchema(String),
    #[error("unterminated SQL construct: {0}")]
    UnterminatedSql(String),
    #[error("unsupported PostgreSQL schema state: {0}")]
    Unsupported(String),
    #[error("schema apply lock: {0}")]
    ApplyLock(String),
    #[error("schema verification failed: {0}")]
    Verification(String),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    BackupManifest(#[from] BackupManifestError),
    #[error(transparent)]
    Postgres(#[from] postgres::Error),
}
