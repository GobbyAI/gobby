use super::{account_home_for_uid, operator_bootstrap_for_home, operator_bootstrap_path};
use std::path::Path;

/// A uid that no passwd database assigns. Fixed, so the test never depends on a real account.
const UNASSIGNED_UID: libc::uid_t = 2_147_483_000;

#[test]
fn operator_bootstrap_is_the_gobby_bootstrap_under_the_account_home() {
    assert_eq!(
        operator_bootstrap_for_home(Path::new("/srv/operator")),
        Path::new("/srv/operator/.gobby/bootstrap.yaml")
    );
}

#[test]
fn a_uid_without_a_passwd_entry_resolves_to_no_operator_root() {
    let home = account_home_for_uid(UNASSIGNED_UID).expect("a missing entry is not an error");
    assert_eq!(home, None);
}

#[test]
fn the_operator_root_is_the_passwd_home_of_the_current_account() {
    // SAFETY: getuid cannot fail and takes no pointers.
    let uid = unsafe { libc::getuid() };
    let home = account_home_for_uid(uid)
        .expect("passwd lookup succeeds")
        .expect("the test account has a passwd entry");
    assert!(home.is_absolute(), "{}", home.display());
    assert_eq!(
        operator_bootstrap_path().expect("operator lookup succeeds"),
        Some(home.join(".gobby").join("bootstrap.yaml"))
    );
}
