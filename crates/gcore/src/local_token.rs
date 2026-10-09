use std::path::Path;

use anyhow::Context as _;

pub const AUTHORIZATION_HEADER: &str = "Authorization";
pub const AGENT_API_TOKEN_ENV: &str = "GOBBY_AGENT_API_TOKEN";

pub fn read_api_key_at(gobby_home: &Path) -> anyhow::Result<String> {
    let path = gobby_home.join("bootstrap.yaml");
    crate::bootstrap::read_hub_database_bootstrap_file(&path)
        .with_context(|| format!("cannot read API key bootstrap at {}", path.display()))?
        .and_then(|bootstrap| bootstrap.api_key)
        .with_context(|| {
            format!(
                "missing API key in {}; run `gobby auth login`",
                path.display()
            )
        })
}

pub fn read_api_key() -> anyhow::Result<String> {
    read_api_key_for(&crate::gobby_home()?)
}

/// Resolve the daemon credential for an explicit Gobby home.
///
/// Sandboxed agent runs deny the operator token file; the run-scoped
/// capability in the environment is their only daemon credential. Callers that
/// already know the home must go through here rather than
/// [`read_api_key_at`], or an agent whose home legitimately carries no
/// token file sends an unauthenticated request and the daemon answers 401.
pub fn read_api_key_for(gobby_home: &Path) -> anyhow::Result<String> {
    if let Some(token) = managed_api_token()? {
        return Ok(token);
    }
    if let Some(token) = agent_api_token_from_env() {
        return Ok(token);
    }
    if std::env::var_os("GOBBY_MANAGED_EXECUTION_BOOTSTRAP").is_some() {
        anyhow::bail!("managed capability is unavailable");
    }
    read_api_key_at(gobby_home)
}

pub(crate) fn managed_token_at(
    path: &Path,
    execution_id: Option<&str>,
    session_id: Option<&str>,
    project_id: Option<&str>,
) -> anyhow::Result<Option<String>> {
    let invalid = || anyhow::anyhow!("managed capability envelope is unavailable or invalid");
    let file = std::fs::File::open(path).map_err(|_| invalid())?;
    let metadata = file.metadata().map_err(|_| invalid())?;
    if !metadata.is_file() {
        return Err(invalid());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err(invalid());
        }
    }
    let value: serde_json::Value = serde_json::from_reader(file).map_err(|_| invalid())?;
    let object = value.as_object().ok_or_else(invalid)?;
    let Some(token) = object.get("managed_api_token") else {
        return Ok(None);
    };
    let token = token
        .as_str()
        .filter(|token| !token.trim().is_empty())
        .ok_or_else(invalid)?;
    let principal = object
        .get("principal")
        .and_then(serde_json::Value::as_object)
        .ok_or_else(invalid)?;
    let execution_id = execution_id
        .filter(|value| !value.is_empty())
        .ok_or_else(invalid)?;
    if principal
        .get("execution_id")
        .and_then(serde_json::Value::as_str)
        != Some(execution_id)
    {
        return Err(invalid());
    }
    for (key, expected) in [("session_id", session_id), ("project_id", project_id)] {
        if let Some(expected) = expected.filter(|value| !value.is_empty())
            && principal.get(key).and_then(serde_json::Value::as_str) != Some(expected)
        {
            return Err(invalid());
        }
    }
    Ok(Some(token.trim().to_owned()))
}

pub(crate) fn managed_api_token() -> anyhow::Result<Option<String>> {
    let Some(path) = std::env::var_os("GOBBY_MANAGED_EXECUTION_BOOTSTRAP") else {
        return Ok(None);
    };
    let run = std::env::var("GOBBY_AGENT_RUN_ID")
        .ok()
        .filter(|value| !value.trim().is_empty());
    let execution = std::env::var("GOBBY_MANAGED_EXECUTION_ID")
        .ok()
        .filter(|value| !value.trim().is_empty());
    if run.is_some() && execution.is_some() {
        anyhow::bail!("managed capability envelope owner is ambiguous");
    }
    managed_token_at(
        Path::new(&path),
        run.as_deref().or(execution.as_deref()).map(str::trim),
        std::env::var("GOBBY_SESSION_ID")
            .ok()
            .as_deref()
            .map(str::trim),
        std::env::var("GOBBY_PROJECT_ID")
            .ok()
            .as_deref()
            .map(str::trim),
    )
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

pub fn authorization_bearer(token: &str) -> String {
    format!("Bearer {token}")
}

pub fn apply_bearer_header(request: ureq::Request) -> ureq::Request {
    let token = read_api_key().ok();
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
    #[test]
    fn bootstrap_api_key_is_read_fresh() -> anyhow::Result<()> {
        let home = tempfile::tempdir()?;
        let path = home.path().join("bootstrap.yaml");
        std::fs::write(&path, "api_key: first-key\n")?;
        assert_eq!(super::read_api_key_at(home.path())?, "first-key");
        std::fs::write(&path, "api_key: second-key\n")?;
        assert_eq!(super::read_api_key_at(home.path())?, "second-key");
        for contents in [
            "api_key: ''\n",
            "api_key: null\n",
            "api_key: 42\n",
            "not: [valid",
        ] {
            std::fs::write(&path, contents)?;
            assert!(super::read_api_key_at(home.path()).is_err(), "{contents}");
        }
        std::fs::remove_file(&path)?;
        let error = super::read_api_key_at(home.path()).expect_err("missing bootstrap");
        assert!(error.to_string().contains("bootstrap"));
        Ok(())
    }
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

        // Env capability wins over the bootstrap API key.
        std::fs::write(home.path().join("bootstrap.yaml"), "api_key: file-token\n")?;
        set_env(AGENT_API_TOKEN_ENV, Some(" env-token "));
        assert_eq!(read_api_key()?, "env-token");

        // Empty or whitespace-only env falls back to bootstrap.
        set_env(AGENT_API_TOKEN_ENV, Some("   "));
        assert_eq!(read_api_key()?, "file-token");
        set_env(AGENT_API_TOKEN_ENV, None);
        assert_eq!(read_api_key()?, "file-token");

        // Both absent: fail with login guidance.
        std::fs::remove_file(home.path().join("bootstrap.yaml"))?;
        let err = read_api_key().expect_err("no credential available");
        assert!(err.to_string().contains("missing API key"));

        set_env(AGENT_API_TOKEN_ENV, saved_token.as_deref());
        set_env("GOBBY_HOME", saved_home.as_deref());
        Ok(())
    }

    #[test]
    fn env_capability_preferred_for_an_explicit_home() -> anyhow::Result<()> {
        // A sandboxed agent's home legitimately carries no bootstrap API key. Callers
        // that pass the home explicitly must still honor the run-scoped
        // capability, or the daemon answers 401 (#19458).
        let _lock = crate::config::TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let home = tempfile::tempdir()?;
        let saved_token = std::env::var(AGENT_API_TOKEN_ENV).ok();

        set_env(AGENT_API_TOKEN_ENV, Some(" env-token "));
        assert_eq!(read_api_key_for(home.path())?, "env-token");

        set_env(AGENT_API_TOKEN_ENV, None);
        let err = read_api_key_for(home.path()).expect_err("no credential available");
        assert!(err.to_string().contains("missing API key"));

        std::fs::write(home.path().join("bootstrap.yaml"), "api_key: file-token\n")?;
        assert_eq!(read_api_key_for(home.path())?, "file-token");

        set_env(AGENT_API_TOKEN_ENV, saved_token.as_deref());
        Ok(())
    }

    #[test]
    #[cfg(unix)]
    fn managed_envelope_token_is_fresh_private_and_owned() -> anyhow::Result<()> {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir()?;
        let path = temp.path().join("grant.json");
        let write = |token: &str| -> anyhow::Result<()> {
            std::fs::write(
                &path,
                serde_json::to_vec(&serde_json::json!({
                    "managed_api_token": token,
                    "principal": {"kind":"agent_run", "execution_id":"run", "session_id":"seat", "project_id":"project"}
                }))?,
            )?;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
            Ok(())
        };
        write("first")?;
        assert_eq!(
            managed_token_at(&path, Some("run"), Some("seat"), Some("project"))?,
            Some("first".to_owned())
        );
        write("renewed")?;
        assert_eq!(
            managed_token_at(&path, Some("run"), Some("seat"), Some("project"))?,
            Some("renewed".to_owned())
        );
        assert!(managed_token_at(&path, Some("other"), Some("seat"), Some("project")).is_err());
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o640))?;
        assert!(managed_token_at(&path, Some("run"), Some("seat"), Some("project")).is_err());
        Ok(())
    }
}
