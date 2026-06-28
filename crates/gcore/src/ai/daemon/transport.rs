use reqwest::blocking::{Client, RequestBuilder};

use crate::ai_types::AiError;

const LOCAL_CLI_TOKEN_FILENAME: &str = "local_cli_token";
pub(super) const LOCAL_TOKEN_HEADER: &str = "X-Gobby-Local-Token";

pub(crate) fn daemon_client() -> Result<Client, AiError> {
    Client::builder()
        .build()
        .map_err(super::super::reqwest_error)
}

pub(crate) fn daemon_url(path: &str) -> String {
    format!(
        "{}{}",
        crate::daemon_url::daemon_url().trim_end_matches('/'),
        path
    )
}

pub(crate) fn read_local_cli_token() -> Result<String, AiError> {
    let path = gobby_home()?.join(LOCAL_CLI_TOKEN_FILENAME);
    let token = std::fs::read_to_string(&path).map_err(|error| {
        AiError::not_configured(
            None,
            format!("missing local CLI token at {}: {}", path.display(), error),
        )
    })?;
    let token = token.trim().to_string();
    if token.is_empty() {
        return Err(AiError::not_configured(
            None,
            format!("local CLI token at {} is empty", path.display()),
        ));
    }
    Ok(token)
}

fn gobby_home() -> Result<std::path::PathBuf, AiError> {
    crate::gobby_home().map_err(|error| AiError::not_configured(None, error.to_string()))
}

pub(crate) fn with_local_token(request: RequestBuilder, token: &str) -> RequestBuilder {
    request.header(LOCAL_TOKEN_HEADER, token)
}
