use super::*;
use crate::grant::{DirectConnections, managed_direct_grant};
use crate::session_credential::{cli_pid_key, store};

const SECRET_TOKEN: &str = "session-token-secret";

fn record(session_id: &str) -> SessionCredential {
    SessionCredential {
        token: SECRET_TOKEN.to_string(),
        session_id: session_id.to_string(),
        project_id: "project-1".to_string(),
        provider_session_id: String::new(),
    }
}

fn interactive_grant(session_id: Option<&str>) -> GrantBundle {
    let connections = DirectConnections::postgres("postgresql://unused/gobby");
    let mut grant = managed_direct_grant("project-1", "machine-1", &connections);
    grant.principal.kind = PrincipalKind::Interactive;
    grant.principal.execution_id = None;
    grant.principal.session_id = session_id.map(str::to_string);
    grant
}

#[test]
fn own_session_record_presents_its_token() {
    let bearer = session_bearer(Some(record("sess-a")), "sess-a").expect("own record presents");
    assert_eq!(bearer, SECRET_TOKEN);
}

#[test]
fn foreign_session_record_is_refused_without_its_token() {
    let error = session_bearer(Some(record("sess-b")), "sess-a").expect_err("foreign refused");
    assert_eq!(
        error,
        GrantError::SessionCredentialRefused(FOREIGN_CREDENTIAL)
    );
    assert!(!error.to_string().contains(SECRET_TOKEN));
}

#[test]
fn missing_record_is_refused() {
    let error = session_bearer(None, "sess-a").expect_err("missing refused");
    assert_eq!(error, GrantError::SessionCredentialRefused(NO_CREDENTIAL));
}

#[test]
fn session_less_interactive_grant_keeps_the_machine_token() {
    let grant = interactive_grant(None);
    let bearer = presentation_bearer(Some(&grant), "machine-key").expect("operator presents");
    assert_eq!(bearer, "machine-key");
}

#[test]
fn managed_grant_keeps_the_machine_token() {
    let connections = DirectConnections::postgres("postgresql://unused/gobby");
    let grant = managed_direct_grant("project-1", "machine-1", &connections);
    let bearer = presentation_bearer(Some(&grant), "machine-key").expect("managed presents");
    assert_eq!(bearer, "machine-key");
}

#[test]
fn absent_grant_keeps_the_machine_token() {
    let bearer = presentation_bearer(None, "machine-key").expect("no grant presents");
    assert_eq!(bearer, "machine-key");
}

#[test]
fn process_record_reads_only_the_record_under_its_cli_pid() {
    let home = tempfile::tempdir().expect("tempdir");
    store(home.path(), &[cli_pid_key(4242)], &record("sess-a")).expect("store record");

    let found = process_record(Some(home.path()), Some(4242), false).expect("own pid record");
    assert_eq!(found.session_id, "sess-a");
    assert!(process_record(Some(home.path()), Some(4343), false).is_none());
    assert!(process_record(Some(home.path()), None, false).is_none());
    assert!(process_record(None, Some(4242), false).is_none());
}

#[test]
fn managed_environment_never_presents_a_session_record() {
    let home = tempfile::tempdir().expect("tempdir");
    store(home.path(), &[cli_pid_key(4242)], &record("sess-a")).expect("store record");

    assert!(process_record(Some(home.path()), Some(4242), true).is_none());
}
