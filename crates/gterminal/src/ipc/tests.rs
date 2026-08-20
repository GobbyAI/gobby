use super::*;
#[cfg(windows)]
use interprocess::local_socket::traits::Listener as _;
#[cfg(windows)]
use std::path::PathBuf;

#[test]
fn stale_socket_connect_errors_keep_unix_would_block_strict() {
    assert!(stale_socket_connect_error(io::ErrorKind::ConnectionRefused));
    assert!(stale_socket_connect_error(io::ErrorKind::NotFound));
    assert!(stale_socket_connect_error(io::ErrorKind::TimedOut));
    assert_eq!(
        stale_socket_connect_error(io::ErrorKind::WouldBlock),
        cfg!(windows)
    );
}

#[cfg(windows)]
#[test]
fn remove_socket_file_if_owned_compares_windows_marker_contents() {
    let path = temp_socket_marker_path("same-len-marker");
    let _ = fs::remove_file(&path);

    fs::write(&path, b"marker-aa").expect("write first marker");
    let identity = socket_file_identity(&path).expect("read first identity");
    fs::write(&path, b"marker-bb").expect("replace with same-length marker");

    remove_socket_file_if_owned(&path, &identity).expect("remove owned marker");

    assert!(path.exists(), "same-length replacement marker must survive");

    let _ = fs::remove_file(&path);
}

#[cfg(windows)]
#[test]
fn idle_named_pipe_peer_is_not_treated_as_closed() {
    let path = temp_socket_marker_path("idle-pipe");
    let listener = bind_local_listener(&path).unwrap();
    let _client = connect_local_stream(&path).unwrap();
    let mut server = listener.accept().unwrap();

    assert!(!local_stream_peer_closed(&mut server).unwrap());

    let _ = fs::remove_file(path);
}

#[cfg(windows)]
#[test]
fn disconnected_named_pipe_peer_is_treated_as_closed() {
    let path = temp_socket_marker_path("disconnected-pipe");
    let listener = bind_local_listener(&path).unwrap();
    let client = connect_local_stream(&path).unwrap();
    let mut server = listener.accept().unwrap();

    drop(client);

    assert!(local_stream_peer_closed(&mut server).unwrap());

    let _ = fs::remove_file(path);
}

#[cfg(windows)]
fn temp_socket_marker_path(name: &str) -> PathBuf {
    std::env::temp_dir().join(format!("gterm-{name}-{}.sock", std::process::id()))
}
