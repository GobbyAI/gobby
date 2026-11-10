use std::time::Duration;

use super::index_lock_policy;
use crate::index_lock::IndexLockPolicy;

#[test]
fn hook_lock_wait_is_bounded_by_the_requested_seconds() {
    assert_eq!(
        index_lock_policy(false, Some(300)),
        IndexLockPolicy::Wait {
            max_wait: Duration::from_secs(300),
        }
    );
}

#[test]
fn an_index_run_without_a_bound_keeps_the_default_wait() {
    assert_eq!(index_lock_policy(false, None), IndexLockPolicy::wait());
}

#[test]
fn skip_if_locked_stays_a_single_try() {
    assert_eq!(
        index_lock_policy(true, None),
        IndexLockPolicy::brief_index_flush_try()
    );
}
