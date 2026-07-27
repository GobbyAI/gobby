use std::path::Path;

use anyhow::{Context as _, bail};

pub const LOCAL_CLI_TOKEN_FILENAME: &str = "local_cli_token";
pub const AUTHORIZATION_HEADER: &str = "Authorization";

pub fn read_local_cli_token() -> anyhow::Result<String> {
    read_local_cli_token_at(&crate::gobby_home()?)
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

#[cfg(feature = "ai")]
pub fn apply_bearer_header(request: ureq::Request) -> ureq::Request {
    let token = read_local_cli_token().ok();
    apply_bearer_header_with_token(request, token.as_deref())
}

#[cfg(feature = "ai")]
pub fn apply_bearer_header_with_token(
    request: ureq::Request,
    token: Option<&str>,
) -> ureq::Request {
    match token {
        Some(token) => request.set(AUTHORIZATION_HEADER, &authorization_bearer(token)),
        None => request,
    }
}
