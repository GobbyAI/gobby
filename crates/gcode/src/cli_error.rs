//! Typed public CLI errors for grant-gated dispatch.

use std::fmt;

use gobby_core::grant::GrantError;
use serde_json::json;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliError {
    pub code: &'static str,
    pub message: String,
    pub recovery: Option<&'static str>,
    pub exit_status: u8,
}

impl CliError {
    pub fn grant(error: GrantError) -> Self {
        Self {
            code: error.cli_code(),
            message: error.to_string(),
            recovery: None,
            exit_status: error.exit_status() as u8,
        }
    }

    pub fn project_required() -> Self {
        Self {
            code: "project_required",
            message: "project required".to_string(),
            recovery: None,
            exit_status: 2,
        }
    }

    pub fn capability_unavailable(capability: &str) -> Self {
        Self {
            code: "capability_unavailable",
            message: format!("{capability} capability is unavailable"),
            recovery: None,
            exit_status: 2,
        }
    }

    pub(crate) fn json_payload(&self) -> serde_json::Value {
        match self.recovery {
            Some(recovery) => json!({
                "error": self.code,
                "message": self.message,
                "recovery": recovery,
            }),
            None => json!({
                "error": self.code,
                "message": self.message,
            }),
        }
    }

    pub fn print(&self) -> anyhow::Result<()> {
        eprintln!("{}", serde_json::to_string(&self.json_payload())?);
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
