//! A loaded debug host may bind after the ordinary operation deadline.

#![cfg(unix)]

mod host_support;

use host_support::{temp_socket_dir, wait_socket_with, CONTROL_SOCKET};
use std::cell::{Cell, RefCell};
use std::os::unix::net::UnixListener;
use std::time::Duration;

#[test]
fn ordinary_hosts_reuse_the_built_image_with_isolated_state() {
    use sha2::{Digest, Sha256};

    let binary = host_support::gterm_bin();
    let original = std::fs::read(&binary).expect("built gterm image");
    let hash = format!("{:x}", Sha256::digest(&original));
    let dirs = [temp_socket_dir(), temp_socket_dir()];
    let mut hosts = Vec::new();
    for dir in &dirs {
        host_support::write_token(dir.path(), "shared-image-token");
        hosts.push(host_support::spawn_host(dir.path()));
        let socket = dir.path().join(CONTROL_SOCKET);
        host_support::wait_socket(&socket);
        let mut control = host_support::connect(&socket);
        host_support::hello_control(&mut control, "shared-image-token");
        let ping = host_support::rpc(&mut control, "ping", serde_json::json!({}));
        assert_eq!(ping["binary_sha256"], hash, "{ping}");
    }
    for dir in &dirs {
        assert!(
            !dir.path().join("gterm").exists(),
            "ordinary host launches must reuse the built executable"
        );
    }
    assert_eq!(
        std::fs::read(&binary).expect("unchanged built image"),
        original
    );
}

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
