use super::{kill_group, KillGroupError};

#[test]
fn kill_group_refuses_non_positive_pgid() {
    assert!(matches!(
        kill_group(0, libc::SIGTERM),
        Err(KillGroupError::InvalidPgid)
    ));
    assert!(matches!(
        kill_group(-1, libc::SIGKILL),
        Err(KillGroupError::InvalidPgid)
    ));
}
