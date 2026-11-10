use super::*;
use std::os::unix::fs::symlink;

const GOBBY: &str = "/gobby-home";
const SEAT_HOME: &str = "/seat-home";
const OPERATOR_BOOTSTRAP: &str = "/operator-home/.gobby/bootstrap.yaml";

/// The pre-memo algorithm: canonicalize the longest existing prefix, re-append the rest.
fn reference_resolve(path: &Path) -> PathBuf {
    let normal = lexical_normal(path);
    let mut existing = normal.clone();
    let mut rest: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(canonical) = existing.canonicalize() {
            let mut out = canonical;
            for part in rest.iter().rev() {
                out.push(part);
            }
            return out;
        }
        match (existing.file_name(), existing.parent()) {
            (Some(name), Some(parent)) => {
                rest.push(name.to_os_string());
                existing = parent.to_path_buf();
            }
            _ => return normal,
        }
    }
}

fn operator_entry(home: &Path) -> String {
    deny_paths([text(&PathBuf::from(OPERATOR_BOOTSTRAP))], None, home)
        .into_iter()
        .next()
        .expect("the operator entry denies one path")
}

#[test]
fn resolver_matches_reference_for_every_path_shape() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = resolve_nonstrict(dir.path());
    let real = root.join("real");
    fs::create_dir(&real).expect("real directory");
    symlink(&real, root.join("link")).expect("directory symlink");
    symlink(root.join("missing"), root.join("dangling")).expect("dangling symlink");
    fs::write(root.join("file.txt"), b"").expect("regular file");

    let cases = [
        real.join("missing").join("deep.json"),
        root.join("link").join("missing.json"),
        root.join("dangling").join("child"),
        root.join("file.txt").join("child"),
        root.join("absent").join("a").join("b").join("c"),
        root.join("link"),
        root.join("dangling"),
        root.join("file.txt"),
        real.clone(),
    ];
    // One resolver serves the whole list, forward then reverse, so memoized parents are
    // reused both before and after their children are resolved.
    let mut resolver = Resolver::default();
    for case in cases.iter().chain(cases.iter().rev()) {
        let expected = reference_resolve(case);
        assert_eq!(
            resolver.resolve(case),
            expected,
            "memoized resolution of {} diverged",
            case.display()
        );
        assert_eq!(
            resolve_nonstrict(case),
            expected,
            "resolve_nonstrict of {} diverged",
            case.display()
        );
    }
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
