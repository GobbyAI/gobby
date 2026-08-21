//! 3.3.12 RAII terminal restore.

use gobby_client::teardown::{RecordingBackend, TerminalGuard};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::Ordering;

#[test]
fn guard_restores_on_quit_failure_panic_and_signal() {
    let (guard, hits) = TerminalGuard::recording();
    drop(guard);
    assert_eq!(hits.load(Ordering::SeqCst), 1);

    let (mut guard, hits) = TerminalGuard::recording();
    guard.inject_startup_failure();
    drop(guard);
    assert_eq!(
        hits.load(Ordering::SeqCst),
        1,
        "startup failure still restores"
    );

    let hits = {
        let (guard, hits) = TerminalGuard::recording();
        let _ = catch_unwind(AssertUnwindSafe(|| {
            let _guard = guard;
            panic!("injected");
        }));
        hits
    };
    assert_eq!(hits.load(Ordering::SeqCst), 1, "panic restores");

    let (guard, hits) = TerminalGuard::recording();
    guard.handle_signal();
    assert_eq!(hits.load(Ordering::SeqCst), 1);
    drop(guard);
    assert_eq!(hits.load(Ordering::SeqCst), 1, "second drop is a no-op");

    let backend = RecordingBackend::default();
    let hits = backend.hits();
    let mut guard = TerminalGuard::new(backend);
    let _ = guard.arm();
    guard.disarm_for_test();
    drop(guard);
    assert_eq!(hits.load(Ordering::SeqCst), 0);
}
