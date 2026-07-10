use reqwest::blocking::{Client, RequestBuilder};

use crate::ai_types::AiError;
use crate::local_token::{
    AUTHORIZATION_HEADER, authorization_bearer, read_local_cli_token as read_shared_local_cli_token,
};

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
    read_shared_local_cli_token().map_err(|error| AiError::not_configured(None, error.to_string()))
}

pub(crate) fn with_local_token(request: RequestBuilder, token: &str) -> RequestBuilder {
    request.header(AUTHORIZATION_HEADER, authorization_bearer(token))
}
