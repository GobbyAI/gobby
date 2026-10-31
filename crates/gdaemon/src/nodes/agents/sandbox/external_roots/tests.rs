use super::validate_external_write_roots;
use std::{collections::BTreeMap, fs, path::Path};
use tempfile::TempDir;

fn text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

fn check(roots: &[String], gobby: &Path, home: &Path) -> Result<(), String> {
    validate_external_write_roots(roots, gobby, home, &BTreeMap::new())
}

#[test]
fn accepts_an_existing_directory_outside_gobby_state() {
    let home = TempDir::new().unwrap();
    let root = TempDir::new().unwrap();
    let gobby = home.path().join(".gobby");
    check(&[text(root.path())], &gobby, home.path()).expect("an existing directory is grantable");
}

#[test]
fn refuses_a_root_missing_on_this_node() {
    let home = TempDir::new().unwrap();
    let missing = home.path().join("absent");
    let error = check(&[text(&missing)], &home.path().join(".gobby"), home.path()).unwrap_err();
    assert!(error.contains("cannot resolve"), "{error}");
    assert!(error.contains(&text(&missing)), "{error}");
}

#[test]
fn refuses_a_root_that_is_not_a_directory() {
    let home = TempDir::new().unwrap();
    let file = home.path().join("note.txt");
    fs::write(&file, "note").unwrap();
    let error = check(&[text(&file)], &home.path().join(".gobby"), home.path()).unwrap_err();
    assert!(error.contains("existing directory"), "{error}");
}

#[test]
fn refuses_a_relative_root() {
    let home = TempDir::new().unwrap();
    let error = check(
        &["relative/root".to_string()],
        &home.path().join(".gobby"),
        home.path(),
    )
    .unwrap_err();
    assert!(error.contains("absolute"), "{error}");
}

#[test]
fn refuses_the_home_root() {
    let home = TempDir::new().unwrap();
    let error = check(
        &[text(home.path())],
        &home.path().join(".gobby"),
        home.path(),
    )
    .unwrap_err();
    assert!(error.contains("home"), "{error}");
}

#[test]
fn refuses_the_filesystem_root() {
    let home = TempDir::new().unwrap();
    let error = check(&["/".to_string()], &home.path().join(".gobby"), home.path()).unwrap_err();
    assert!(error.contains("Filesystem"), "{error}");
}

#[test]
fn refuses_a_root_that_contains_protected_gobby_state() {
    let home = TempDir::new().unwrap();
    let gobby = home.path().join(".gobby");
    fs::create_dir_all(gobby.join("gcode-runtime")).unwrap();
    let error = check(&[text(&gobby)], &gobby, home.path()).unwrap_err();
    assert!(error.contains("protected root"), "{error}");
}
