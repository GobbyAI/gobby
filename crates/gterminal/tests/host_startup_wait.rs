//! A loaded debug host may bind after the ordinary operation deadline.

#![cfg(unix)]

mod host_support;

use host_support::{temp_socket_dir, wait_socket_with, CONTROL_SOCKET};
use std::cell::{Cell, RefCell};
use std::os::unix::net::UnixListener;
use std::time::Duration;

#[test]
fn socket_startup_tolerates_bind_after_fifteen_seconds() {
    let dir = temp_socket_dir();
    let socket = dir.path().join(CONTROL_SOCKET);
    let elapsed = Cell::new(Duration::ZERO);
    let listener = RefCell::new(None);
    wait_socket_with(
        &socket,
        || elapsed.get(),
        || {
            elapsed.set(elapsed.get() + Duration::from_secs(1));
            if elapsed.get() == Duration::from_secs(16) {
                *listener.borrow_mut() = Some(UnixListener::bind(&socket).expect("delayed bind"));
            }
        },
    );
    let listener = listener
        .into_inner()
        .expect("listener bound after 16 seconds");
    assert_eq!(
        listener.local_addr().unwrap().as_pathname(),
        Some(socket.as_path())
    );
}

#[test]
fn socket_startup_stays_bounded_and_reports_timeout_context() {
    let dir = temp_socket_dir();
    let socket = dir.path().join(CONTROL_SOCKET);
    // An existing pathname alone must never count as a listening socket.
    std::fs::write(&socket, b"not a socket").expect("stale path");
    let elapsed = Cell::new(Duration::ZERO);
    let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        wait_socket_with(
            &socket,
            || elapsed.get(),
            || elapsed.set(elapsed.get() + Duration::from_secs(1)),
        );
    }))
    .expect_err("a non-listening path must time out");
    let message = panic.downcast_ref::<String>().expect("timeout message");
    for field in [
        "elapsed=60s",
        "budget=60s",
        "exists=true",
        "spawned_pid=",
        "state=",
        "log=",
        "stderr=",
    ] {
        assert!(message.contains(field), "missing {field}: {message}");
    }
    assert_eq!(elapsed.get(), Duration::from_secs(60));
}
