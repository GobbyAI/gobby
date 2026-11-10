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
    assert_eq!(roots.len(), 9);
    assert!(roots.iter().all(|root| root.starts_with(GOBBY)));
}

#[test]
fn credential_roots_deny_the_signing_key_its_staging_and_the_session_store() {
    let roots = credential_roots(Path::new(GOBBY), None);
    for name in [
        "capability_signing_key",
        ".capability_signing_key-staging",
        "session-credentials",
    ] {
        assert!(
            roots.contains(&Path::new(GOBBY).join(name)),
            "{name} missing from credential roots: {roots:?}"
        );
    }
}

#[test]
fn read_and_write_denies_cover_the_signing_key_and_session_store() {
    let home = Path::new(SEAT_HOME);
    let reads = sensitive_roots(Path::new(GOBBY), home, &BTreeMap::new(), None);
    let writes = sensitive_write_roots(Path::new(GOBBY), home, &BTreeMap::new(), None);
    for name in ["capability_signing_key", "session-credentials"] {
        let entry = deny_paths([text(&Path::new(GOBBY).join(name))], None, home)
            .into_iter()
            .next()
            .expect("one deny entry");
        assert!(
            reads.contains(&entry),
            "{name} missing from read denies: {reads:?}"
        );
        assert!(
            writes.contains(&entry),
            "{name} missing from write denies: {writes:?}"
        );
    }
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
