use reqwest::blocking::{Client, RequestBuilder};

use crate::ai_types::AiError;
use crate::grant::{
    AGENT_RUN_HEADER, CALLER_PROJECT_HEADER, GRANT_HEADER, GrantBundle, MACHINE_HEADER,
    MANAGED_EXECUTION_HEADER, PrincipalKind, SESSION_HEADER, TARGET_PROJECT_HEADER,
    encode_grant_header,
};
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

pub fn grant_presentation_headers(
    grant: &GrantBundle,
) -> Result<Vec<(&'static str, String)>, AiError> {
    let mut headers = vec![
        (
            GRANT_HEADER,
            encode_grant_header(grant).map_err(|error| {
                AiError::not_configured(None, format!("grant header encode failed: {error}"))
            })?,
        ),
        (MACHINE_HEADER, grant.principal.machine_id.clone()),
        (CALLER_PROJECT_HEADER, grant.principal.project_id.clone()),
        (TARGET_PROJECT_HEADER, grant.principal.project_id.clone()),
    ];
    if let Some(session_id) = &grant.principal.session_id {
        headers.push((SESSION_HEADER, session_id.clone()));
    }
    if let Some(execution_id) = &grant.principal.execution_id {
        let name = match grant.principal.kind {
            PrincipalKind::ToolChat | PrincipalKind::Maintenance => MANAGED_EXECUTION_HEADER,
            PrincipalKind::AgentRun | PrincipalKind::Interactive => AGENT_RUN_HEADER,
        };
        headers.push((name, execution_id.clone()));
    }
    Ok(headers)
}

pub(crate) fn with_grant_presentation(
    request: RequestBuilder,
    token: &str,
    grant: &GrantBundle,
) -> Result<RequestBuilder, AiError> {
    let mut request = with_local_token(request, token);
    for (name, value) in grant_presentation_headers(grant)? {
        request = request.header(name, value);
    }
    Ok(request)
}
