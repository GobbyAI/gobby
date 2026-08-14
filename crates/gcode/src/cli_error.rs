//! Typed public CLI errors for grant-gated dispatch.

use std::fmt;

use gobby_core::grant::GrantError;
use serde_json::json;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliError {
    pub code: &'static str,
    pub message: String,
    pub exit_status: u8,
}

impl CliError {
    pub fn grant(error: GrantError) -> Self {
        Self {
            code: error.cli_code(),
            message: error.to_string(),
            exit_status: error.exit_status() as u8,
        }
    }

    pub fn project_required() -> Self {
        Self {
            code: "project_required",
            message: "project required".to_string(),
            exit_status: 2,
        }
    }

    pub fn print(&self) -> anyhow::Result<()> {
        let payload = json!({
            "error": self.code,
            "message": self.message,
        });
        eprintln!("{}", serde_json::to_string(&payload)?);
        Ok(())
    }
}

impl fmt::Display for CliError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for CliError {}

impl From<GrantError> for CliError {
    fn from(error: GrantError) -> Self {
        Self::grant(error)
    }
}
