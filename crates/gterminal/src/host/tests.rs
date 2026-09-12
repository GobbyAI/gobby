use super::*;

#[test]
fn local_token_prefers_the_socket_directory() {
    let root = tempfile::tempdir().expect("tempdir");
    let socket_dir = root.path().join("gterm-host");
    let gobby_home = root.path().join("gobby-home");
    fs::create_dir_all(&socket_dir).expect("socket dir");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    fs::write(socket_dir.join(LOCAL_CLI_TOKEN_FILE), "socket-token\n").expect("socket token");
    fs::write(gobby_home.join(LOCAL_CLI_TOKEN_FILE), "home-token\n").expect("home token");

    assert_eq!(
        read_local_token_from(&socket_dir, Some(gobby_home.as_path())),
        "socket-token"
    );
}

#[test]
fn local_token_falls_back_to_gobby_home() {
    let root = tempfile::tempdir().expect("tempdir");
    let socket_dir = root.path().join("gterm-host");
    let gobby_home = root.path().join("gobby-home");
    fs::create_dir_all(&socket_dir).expect("socket dir");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    fs::write(gobby_home.join(LOCAL_CLI_TOKEN_FILE), "home-token\n").expect("home token");

    assert_eq!(
        read_local_token_from(&socket_dir, Some(gobby_home.as_path())),
        "home-token"
    );
}

#[test]
fn local_token_is_empty_without_any_candidate() {
    let root = tempfile::tempdir().expect("tempdir");
    let socket_dir = root.path().join("gterm-host");
    let gobby_home = root.path().join("gobby-home");
    fs::create_dir_all(&socket_dir).expect("socket dir");
    fs::create_dir_all(&gobby_home).expect("gobby home");

    assert_eq!(
        read_local_token_from(&socket_dir, Some(gobby_home.as_path())),
        ""
    );
}
