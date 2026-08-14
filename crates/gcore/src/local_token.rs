use std::path::Path;

use anyhow::{Context as _, bail};

pub const LOCAL_CLI_TOKEN_FILENAME: &str = "local_cli_token";
pub const AUTHORIZATION_HEADER: &str = "Authorization";
pub const AGENT_API_TOKEN_ENV: &str = "GOBBY_AGENT_API_TOKEN";

pub fn read_local_cli_token() -> anyhow::Result<String> {
    read_local_cli_token_for(&crate::gobby_home()?)
}

/// Resolve the daemon credential for an explicit Gobby home.
///
/// Sandboxed agent runs deny the operator token file; the run-scoped
/// capability in the environment is their only daemon credential. Callers that
/// already know the home must go through here rather than
/// [`read_local_cli_token_at`], or an agent whose home legitimately carries no
/// token file sends an unauthenticated request and the daemon answers 401.
pub fn read_local_cli_token_for(gobby_home: &Path) -> anyhow::Result<String> {
    if let Some(token) = agent_api_token_from_env() {
        return Ok(token);
    }
    read_local_cli_token_at(gobby_home)
}

fn agent_api_token_from_env() -> Option<String> {
    let value = std::env::var(AGENT_API_TOKEN_ENV).ok()?;
    let value = value.trim();
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

pub fn read_local_cli_token_at(gobby_home: &Path) -> anyhow::Result<String> {
    let path = gobby_home.join(LOCAL_CLI_TOKEN_FILENAME);
    let token = std::fs::read_to_string(&path)
        .with_context(|| format!("missing local CLI token at {}", path.display()))?;
    let token = token.trim();
    if token.is_empty() {
        bail!("local CLI token at {} is empty", path.display());
    }
    Ok(token.to_string())
}

pub fn authorization_bearer(token: &str) -> String {
    format!("Bearer {token}")
}

pub fn apply_bearer_header(request: ureq::Request) -> ureq::Request {
    let token = read_local_cli_token().ok();
    apply_bearer_header_with_token(request, token.as_deref())
}

pub fn apply_bearer_header_with_token(
    request: ureq::Request,
    token: Option<&str>,
) -> ureq::Request {
    match token {
        Some(token) => request.set(AUTHORIZATION_HEADER, &authorization_bearer(token)),
        None => request,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set_env(name: &str, value: Option<&str>) {
        // SAFETY: callers hold TEST_ENV_LOCK while mutating and restoring
        // the process environment.
        match value {
            Some(value) => unsafe { std::env::set_var(name, value) },
            None => unsafe { std::env::remove_var(name) },
        }
    }

    #[test]
    fn env_capability_preferred() -> anyhow::Result<()> {
        let _lock = crate::config::TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let home = tempfile::tempdir()?;
        let saved_token = std::env::var(AGENT_API_TOKEN_ENV).ok();
        let saved_home = std::env::var("GOBBY_HOME").ok();
        set_env(
            "GOBBY_HOME",
            Some(home.path().to_str().expect("utf-8 tempdir")),
        );

        // Env capability wins over the token file.
        std::fs::write(home.path().join(LOCAL_CLI_TOKEN_FILENAME), "file-token\n")?;
        set_env(AGENT_API_TOKEN_ENV, Some(" env-token "));
        assert_eq!(read_local_cli_token()?, "env-token");

        // Empty or whitespace-only env falls back to the file.
        set_env(AGENT_API_TOKEN_ENV, Some("   "));
        assert_eq!(read_local_cli_token()?, "file-token");
        set_env(AGENT_API_TOKEN_ENV, None);
        assert_eq!(read_local_cli_token()?, "file-token");

        // Both absent: the file error path is unchanged.
        std::fs::remove_file(home.path().join(LOCAL_CLI_TOKEN_FILENAME))?;
        let err = read_local_cli_token().expect_err("no credential available");
        assert!(err.to_string().contains("missing local CLI token"));

        set_env(AGENT_API_TOKEN_ENV, saved_token.as_deref());
        set_env("GOBBY_HOME", saved_home.as_deref());
        Ok(())
    }

    #[test]
    fn env_capability_preferred_for_an_explicit_home() -> anyhow::Result<()> {
        // A sandboxed agent's home legitimately carries no token file. Callers
        // that pass the home explicitly must still honor the run-scoped
        // capability, or the daemon answers 401 (#19458).
        let _lock = crate::config::TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let home = tempfile::tempdir()?;
        let saved_token = std::env::var(AGENT_API_TOKEN_ENV).ok();

        set_env(AGENT_API_TOKEN_ENV, Some(" env-token "));
        assert_eq!(read_local_cli_token_for(home.path())?, "env-token");

        set_env(AGENT_API_TOKEN_ENV, None);
        let err = read_local_cli_token_for(home.path()).expect_err("no credential available");
        assert!(err.to_string().contains("missing local CLI token"));

        std::fs::write(home.path().join(LOCAL_CLI_TOKEN_FILENAME), "file-token\n")?;
        assert_eq!(read_local_cli_token_for(home.path())?, "file-token");

        set_env(AGENT_API_TOKEN_ENV, saved_token.as_deref());
        Ok(())
    }
}
