//! Python-compatible daemon secret envelope and credential sealing.

use gobby_core::postgres_pool::Pool;
use std::path::{Path, PathBuf};
use std::sync::Arc;

#[derive(Debug, thiserror::Error)]
pub enum SecretError {
    #[error("secret key unavailable")]
    Unavailable,
    #[error("invalid sealed credential")]
    InvalidToken,
}

pub struct SecretStore {
    pool: Arc<Pool>,
    home: PathBuf,
}

impl SecretStore {
    pub fn new(pool: Arc<Pool>, home: PathBuf) -> Self {
        Self { pool, home }
    }
    pub async fn ensure_ready(&self) -> Result<(), SecretError> {
        todo!()
    }
    pub async fn secret(&self, _name: &str) -> Result<Option<String>, SecretError> {
        todo!()
    }
    pub async fn seal(&self, _plaintext: &[u8], _aad: &[u8]) -> Result<String, SecretError> {
        todo!()
    }
    pub async fn open_sealed(&self, _token: &str, _aad: &[u8]) -> Result<Vec<u8>, SecretError> {
        todo!()
    }
}

pub fn publish_private_file(_path: &Path, _data: &[u8]) -> std::io::Result<bool> {
    todo!()
}
