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
            recovery: grant_recovery(&error),
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

fn grant_recovery(error: &GrantError) -> Option<&'static str> {
    match error {
        GrantError::PayloadSkew { .. } | GrantError::ApiContractMismatch { .. } => Some(
            "rebuild and reinstall the ~/.gobby/bin binaries (`gobby install`), or restart the Gobby daemon that matches them",
        ),
        GrantError::DaemonRequired => Some("start the Gobby daemon (`gobby start`)"),
        GrantError::Expired | GrantError::Revoked => Some(
            "re-run the command after the daemon reissues the grant; if it persists, restart the session",
        ),
        GrantError::SchemaMismatch
        | GrantError::DeploymentMismatch
        | GrantError::ConfigRevisionMismatch => {
            Some("restart the Gobby daemon so grants match the installed schema and config")
        }
        GrantError::Timeout
        | GrantError::Malformed(_)
        | GrantError::Io(_)
        | GrantError::RemoteEndpoint => None,
    }
}

#[cfg(test)]
mod tests {
    use super::CliError;
    use gobby_core::grant::GrantError;

    const SKEW_RECOVERY: &str = "rebuild and reinstall the ~/.gobby/bin binaries (`gobby install`), or restart the Gobby daemon that matches them";
    const DAEMON_RECOVERY: &str = "start the Gobby daemon (`gobby start`)";

    #[test]
    fn payload_skew_grant_error_includes_reinstall_recovery() {
        let rendered = CliError::grant(GrantError::PayloadSkew {
            detail: "unknown field `credential_generation`".to_string(),
        });
        assert_eq!(rendered.code, "payload_skew");
        assert_eq!(rendered.exit_status, 2);
        assert_eq!(rendered.recovery, Some(SKEW_RECOVERY));
        let value = rendered.json_payload();
        assert_eq!(value["error"], "payload_skew");
        assert_eq!(
            value["message"],
            "grant payload skew: unknown field `credential_generation`"
        );
        assert_eq!(value["recovery"], SKEW_RECOVERY);
    }

    #[test]
    fn daemon_required_grant_error_includes_start_daemon_recovery() {
        let rendered = CliError::grant(GrantError::DaemonRequired);
        assert_eq!(rendered.code, "daemon_required");
        assert_eq!(rendered.exit_status, 2);
        assert_eq!(rendered.recovery, Some(DAEMON_RECOVERY));
        let value = rendered.json_payload();
        assert_eq!(value["error"], "daemon_required");
        assert_eq!(value["message"], "daemon required");
        assert_eq!(value["recovery"], DAEMON_RECOVERY);
    }

    #[test]
    fn malformed_grant_error_omits_recovery_key() {
        let rendered = CliError::grant(GrantError::Malformed("bad json".to_string()));
        assert_eq!(rendered.code, "malformed");
        assert_eq!(rendered.recovery, None);
        let value = rendered.json_payload();
        assert_eq!(value["error"], "malformed");
        assert_eq!(value["message"], "malformed grant: bad json");
        assert!(
            value.get("recovery").is_none(),
            "recovery must be omitted when no directive exists: {value}"
        );
    }
}
