use super::{credential_roots, deny_paths, sensitive_roots, sensitive_write_roots, text};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

const GOBBY: &str = "/gobby-home";
const SEAT_HOME: &str = "/seat-home";
const OPERATOR_BOOTSTRAP: &str = "/operator-home/.gobby/bootstrap.yaml";

fn operator_entry(home: &Path) -> String {
    deny_paths([text(&PathBuf::from(OPERATOR_BOOTSTRAP))], None, home)
        .into_iter()
        .next()
        .expect("the operator entry denies one path")
}

#[test]
fn credential_roots_end_with_the_operator_bootstrap_when_present() {
    let operator = PathBuf::from(OPERATOR_BOOTSTRAP);
    let roots = credential_roots(Path::new(GOBBY), Some(&operator));
    assert_eq!(roots.last(), Some(&operator));
    assert_eq!(
        roots.len(),
        credential_roots(Path::new(GOBBY), None).len() + 1
    );
}

#[test]
fn credential_roots_without_an_operator_entry_keep_the_gobby_roots() {
    let roots = credential_roots(Path::new(GOBBY), None);
    assert_eq!(roots.len(), 6);
    assert!(roots.iter().all(|root| root.starts_with(GOBBY)));
}

#[test]
fn read_denies_cover_the_operator_bootstrap_not_the_seat_home() {
    let home = Path::new(SEAT_HOME);
    let operator = PathBuf::from(OPERATOR_BOOTSTRAP);
    let denied = sensitive_roots(Path::new(GOBBY), home, &BTreeMap::new(), Some(&operator));
    assert!(
        denied.contains(&operator_entry(home)),
        "operator bootstrap missing from read denies: {denied:?}"
    );
    assert!(
        !denied
            .iter()
            .any(|entry| entry.starts_with("/seat-home/.gobby")),
        "the seat HOME must not supply the bootstrap root: {denied:?}"
    );
}

#[test]
fn write_denies_cover_the_operator_bootstrap() {
    let home = Path::new(SEAT_HOME);
    let operator = PathBuf::from(OPERATOR_BOOTSTRAP);
    let denied = sensitive_write_roots(Path::new(GOBBY), home, &BTreeMap::new(), Some(&operator));
    assert!(
        denied.contains(&operator_entry(home)),
        "operator bootstrap missing from write denies: {denied:?}"
    );
}
