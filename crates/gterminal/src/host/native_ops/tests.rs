use super::{KillGroupError, kill_group};

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
