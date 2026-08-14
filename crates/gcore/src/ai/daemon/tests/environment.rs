use super::super::operations::TEXT_GENERATE_PATH;
use super::super::transport::daemon_url;
use super::*;

#[test]
fn probe_and_transport_resolve_same_custom_port_url_under_gobby_home() {
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), 61999, "parity-token");

    let base = crate::daemon_url::daemon_url();
    assert_eq!(base, "http://127.0.0.1:61999");

    assert_eq!(
        daemon_url(TEXT_GENERATE_PATH),
        format!("{base}{TEXT_GENERATE_PATH}")
    );
}
