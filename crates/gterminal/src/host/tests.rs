use super::*;

#[test]
fn api_key_ignores_the_socket_directory() {
    let root = tempfile::tempdir().expect("tempdir");
    let socket_dir = root.path().join("gterm-host");
    let gobby_home = root.path().join("gobby-home");
    fs::create_dir_all(&socket_dir).expect("socket dir");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    fs::write(socket_dir.join("bootstrap.yaml"), "api_key: socket-key\n").expect("socket key");
    fs::write(gobby_home.join("bootstrap.yaml"), "api_key: home-key\n").expect("home key");

    assert_eq!(
        read_api_key_from(Some(gobby_home.as_path())),
        Some("home-key".into())
    );
}

#[test]
fn api_key_reads_bootstrap_fresh() {
    let root = tempfile::tempdir().expect("tempdir");
    let gobby_home = root.path().join("gobby-home");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    for key in ["first-key", "second-key"] {
        fs::write(
            gobby_home.join("bootstrap.yaml"),
            format!("api_key: ' {key} '\n"),
        )
        .expect("home key");
        assert_eq!(
            read_api_key_from(Some(gobby_home.as_path())),
            Some(key.into())
        );
    }
}

#[test]
fn api_key_is_absent_without_a_usable_bootstrap_key() {
    let root = tempfile::tempdir().expect("tempdir");
    let gobby_home = root.path().join("gobby-home");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    assert_eq!(read_api_key_from(None), None);
    assert_eq!(read_api_key_from(Some(gobby_home.as_path())), None);
    for content in [
        "",
        "api_key: ''\n",
        "api_key: 123\n",
        "api_key: [\n",
        "api_key: null\n",
    ] {
        fs::write(gobby_home.join("bootstrap.yaml"), content).expect("bootstrap");
        assert_eq!(read_api_key_from(Some(gobby_home.as_path())), None);
    }
}
