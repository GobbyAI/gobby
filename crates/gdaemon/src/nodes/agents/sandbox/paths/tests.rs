use super::*;
use std::os::unix::fs::symlink;

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
