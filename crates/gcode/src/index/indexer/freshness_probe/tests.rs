use super::*;
use std::fs::File;
use std::path::PathBuf;

fn write_file(root: &Path, rel: &str, contents: &[u8]) -> PathBuf {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("create parent");
    }
    std::fs::write(&path, contents).expect("write file");
    path
}

fn set_mtime(path: &Path, time: SystemTime) {
    File::options()
        .write(true)
        .open(path)
        .expect("open file to set mtime")
        .set_modified(time)
        .expect("set mtime");
}

/// A fixed, whole-second base instant well in the past, so the arithmetic
/// never underflows and 1-second-granularity filesystems round-trip it.
fn base_time() -> SystemTime {
    SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000)
}

fn default_options() -> walker::DiscoveryOptions {
    walker::DiscoveryOptions::default()
}

#[test]
fn reports_no_change_when_everything_predates_last_index() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    let lib = write_file(root, "src/lib.rs", b"fn main() {}\n");
    let readme = write_file(root, "README.md", b"# Title\n");

    let base = base_time();
    set_mtime(&lib, base);
    set_mtime(&readme, base);

    // last_indexed_at is well after every file's mtime.
    let last = base + Duration::from_secs(3600);
    let indexed = vec!["src/lib.rs".to_string(), "README.md".to_string()];

    assert!(!project_changed_since(
        root,
        last,
        &indexed,
        &[],
        default_options()
    ));
}

#[test]
fn reports_change_when_a_file_is_modified_after_last_index() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    let lib = write_file(root, "src/lib.rs", b"fn main() {}\n");
    set_mtime(&lib, base_time() + Duration::from_secs(7200));

    let last = base_time() + Duration::from_secs(3600);
    let indexed = vec!["src/lib.rs".to_string()];

    assert!(project_changed_since(
        root,
        last,
        &indexed,
        &[],
        default_options()
    ));
}

#[test]
fn reports_change_for_unindexed_file_even_when_mtime_is_old() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    let added = write_file(root, "src/new.rs", b"fn added() {}\n");
    set_mtime(&added, base_time());

    let last = base_time() + Duration::from_secs(3600);
    let indexed: Vec<String> = Vec::new();

    assert!(project_changed_since(
        root,
        last,
        &indexed,
        &[],
        default_options()
    ));
}

#[test]
fn reports_change_when_indexed_file_is_deleted() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    let lib = write_file(root, "src/lib.rs", b"fn main() {}\n");
    set_mtime(&lib, base_time());

    let last = base_time() + Duration::from_secs(3600);
    // "src/gone.rs" is recorded as indexed but no longer exists on disk.
    let indexed = vec!["src/lib.rs".to_string(), "src/gone.rs".to_string()];

    assert!(project_changed_since(
        root,
        last,
        &indexed,
        &[],
        default_options()
    ));
}

#[test]
fn skew_margin_boundary_only_ever_makes_the_gate_more_eager() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    let lib = write_file(root, "src/lib.rs", b"fn main() {}\n");
    let mtime = base_time();
    set_mtime(&lib, mtime);
    let indexed = vec!["src/lib.rs".to_string()];

    // File is 1s older than last_indexed_at — inside the 2s margin, so the
    // gate refreshes (threshold = last - 2s = mtime - 1s < mtime).
    let within_margin = mtime + Duration::from_secs(1);
    assert!(project_changed_since(
        root,
        within_margin,
        &indexed,
        &[],
        default_options()
    ));

    // File sits exactly at the boundary (threshold == mtime, mtime <=
    // threshold), so it counts as unchanged.
    let at_margin = mtime + SKEW_MARGIN;
    assert!(!project_changed_since(
        root,
        at_margin,
        &indexed,
        &[],
        default_options()
    ));

    // File is 3s older than last_indexed_at — beyond the 2s margin, so the
    // gate skips (threshold = last - 2s = mtime + 1s >= mtime).
    let beyond_margin = mtime + Duration::from_secs(3);
    assert!(!project_changed_since(
        root,
        beyond_margin,
        &indexed,
        &[],
        default_options()
    ));
}

#[test]
fn gitignored_new_files_follow_respect_gitignore_setting() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    std::fs::create_dir(root.join(".git")).expect("git dir");
    write_file(root, ".gitignore", b"ignored.rs\n");
    let ignored = write_file(root, "ignored.rs", b"fn ignored() {}\n");
    set_mtime(&ignored, base_time() + Duration::from_secs(7200));

    let last = base_time() + Duration::from_secs(3600);
    let indexed: Vec<String> = Vec::new();

    assert!(!project_changed_since(
        root,
        last,
        &indexed,
        &[],
        walker::DiscoveryOptions {
            respect_gitignore: true
        }
    ));
    assert!(project_changed_since(
        root,
        last,
        &indexed,
        &[],
        walker::DiscoveryOptions {
            respect_gitignore: false
        }
    ));
}

#[test]
fn newly_excluded_indexed_file_triggers_pruning_refresh() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    let generated = write_file(root, "generated/output.rs", b"fn generated() {}\n");
    set_mtime(&generated, base_time());

    let last = base_time() + Duration::from_secs(3600);
    let indexed = vec!["generated/output.rs".to_string()];
    let extra_excludes = vec!["generated".to_string()];

    assert!(project_changed_since(
        root,
        last,
        &indexed,
        &extra_excludes,
        default_options()
    ));
}

/// A watermark safely after every inode change a test makes, so each fixture
/// file's mtime and ctime both predate the probe's threshold.
fn watermark_after_now() -> SystemTime {
    SystemTime::now() + Duration::from_secs(3600)
}

#[test]
fn indexed_file_older_than_the_watermark_is_not_read() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    // The classifier rejects these bytes as binary, so a probe that read the
    // file would drop it from discovery and report the indexed path missing.
    let lib = write_file(root, "src/lib.rs", b"fn lib() {}\0\n");
    set_mtime(&lib, base_time());
    let indexed = vec!["src/lib.rs".to_string()];

    assert!(!project_changed_since(
        root,
        watermark_after_now(),
        &indexed,
        &[],
        default_options()
    ));
}

#[cfg(unix)]
#[test]
fn indexed_file_older_than_the_watermark_is_not_opened() {
    use std::os::unix::fs::PermissionsExt;

    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    let lib = write_file(root, "src/lib.rs", b"fn lib() {}\n");
    set_mtime(&lib, base_time());
    // Opening an unreadable file fails, and the classifier treats that as binary.
    std::fs::set_permissions(&lib, std::fs::Permissions::from_mode(0o000))
        .expect("make file unreadable");
    let indexed = vec!["src/lib.rs".to_string()];

    assert!(!project_changed_since(
        root,
        watermark_after_now(),
        &indexed,
        &[],
        default_options()
    ));
}

#[test]
fn newer_ctime_routes_indexed_file_through_full_classification() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    // Rewriting the file moves its ctime to now, after the watermark. With the
    // mtime backdated (as `cp -p` does), only the ctime sends it to the
    // classifier, which drops the now-binary bytes.
    let lib = write_file(root, "src/lib.rs", b"fn lib() {}\0\n");
    set_mtime(&lib, base_time());
    let last = base_time() + Duration::from_secs(3600);
    let indexed = vec!["src/lib.rs".to_string()];

    assert!(project_changed_since(
        root,
        last,
        &indexed,
        &[],
        default_options()
    ));
}

/// The relative paths the indexer records for `root`: its own discovery,
/// keyed exactly as the probe keys them.
fn indexer_paths(root: &Path, extra_excludes: &[String]) -> Vec<String> {
    let excludes = effective_excludes(extra_excludes);
    let (candidates, content_only) =
        walker::discover_files_with_options(root, &excludes, default_options());
    let mut paths: Vec<String> = candidates
        .iter()
        .chain(content_only.iter())
        .map(|path| relative_path(path, root).expect("relative path"))
        .collect();
    paths.sort();
    paths
}

/// The indexer's verdict: its discovery no longer matches what was recorded,
/// or a file it would read was modified after the threshold.
fn indexer_sees_change(
    root: &Path,
    last: SystemTime,
    indexed: &[String],
    extra_excludes: &[String],
) -> bool {
    let threshold = last - SKEW_MARGIN;
    let discovered = indexer_paths(root, extra_excludes);
    let modified = discovered.iter().any(|rel| {
        root.join(rel)
            .metadata()
            .and_then(|meta| meta.modified())
            .map_or(true, |modified| modified > threshold)
    });
    let mut recorded = indexed.to_vec();
    recorded.sort();
    modified || discovered != recorded
}

struct AgreementCase {
    name: &'static str,
    /// Extra excludes in force when the index was recorded.
    indexed_excludes: &'static [&'static str],
    /// Extra excludes in force when the probe runs.
    probe_excludes: &'static [&'static str],
    mutate: fn(root: &Path, outside: &Path, last: SystemTime),
    expected: bool,
}

fn owned(excludes: &[&str]) -> Vec<String> {
    excludes
        .iter()
        .map(|exclude| (*exclude).to_string())
        .collect()
}

#[cfg(unix)]
#[test]
fn probe_and_indexer_agree_on_every_change_fixture() {
    use std::os::unix::fs::symlink;

    let cases = [
        AgreementCase {
            name: "unchanged",
            indexed_excludes: &[],
            probe_excludes: &[],
            mutate: |_, _, _| {},
            expected: false,
        },
        AgreementCase {
            name: "new file",
            indexed_excludes: &[],
            probe_excludes: &[],
            mutate: |root, _, _| {
                write_file(root, "src/c.rs", b"fn c() {}\n");
            },
            expected: true,
        },
        AgreementCase {
            name: "modified indexed file",
            indexed_excludes: &[],
            probe_excludes: &[],
            mutate: |root, _, last| {
                let a = write_file(root, "src/a.rs", b"fn a() { changed() }\n");
                set_mtime(&a, last + Duration::from_secs(3600));
            },
            expected: true,
        },
        AgreementCase {
            name: "deleted indexed file",
            indexed_excludes: &[],
            probe_excludes: &[],
            mutate: |root, _, _| std::fs::remove_file(root.join("src/b.rs")).expect("remove"),
            expected: true,
        },
        AgreementCase {
            name: "indexed file moved into an exclude glob",
            indexed_excludes: &[],
            probe_excludes: &["docs"],
            mutate: |_, _, _| {},
            expected: true,
        },
        AgreementCase {
            name: "file moved out of an exclude glob",
            indexed_excludes: &["docs"],
            probe_excludes: &[],
            mutate: |_, _, _| {},
            expected: true,
        },
        AgreementCase {
            name: "indexed file replaced by a symlink escaping the root",
            indexed_excludes: &[],
            probe_excludes: &[],
            mutate: |root, outside, _| {
                let target = write_file(outside, "a.rs", b"fn outside() {}\n");
                set_mtime(&target, base_time());
                std::fs::remove_file(root.join("src/a.rs")).expect("remove");
                symlink(&target, root.join("src/a.rs")).expect("symlink");
            },
            expected: true,
        },
        AgreementCase {
            name: "indexed file rewritten as binary",
            indexed_excludes: &[],
            probe_excludes: &[],
            mutate: |root, _, last| {
                let a = write_file(root, "src/a.rs", b"fn a() {}\0\n");
                set_mtime(&a, last + Duration::from_secs(3600));
            },
            expected: true,
        },
        AgreementCase {
            name: "indexed file renamed to a secret extension",
            indexed_excludes: &[],
            probe_excludes: &[],
            mutate: |root, _, _| {
                std::fs::rename(root.join("src/b.rs"), root.join("src/b.pem")).expect("rename");
            },
            expected: true,
        },
        AgreementCase {
            name: "in-root symlink alias of an indexed file",
            indexed_excludes: &[],
            probe_excludes: &[],
            mutate: |root, _, _| {
                symlink(root.join("src/a.rs"), root.join("src/alias.rs")).expect("symlink");
            },
            expected: false,
        },
        AgreementCase {
            name: "allowlisted plan deleted",
            indexed_excludes: &[],
            probe_excludes: &[],
            mutate: |root, _, _| {
                std::fs::remove_file(root.join(".gobby/plans/p.md")).expect("remove");
            },
            expected: true,
        },
    ];

    for case in cases {
        let tmp = tempfile::tempdir().expect("tempdir");
        let outside = tempfile::tempdir().expect("outside tempdir");
        let root = tmp.path();
        for rel in [
            "src/a.rs",
            "src/b.rs",
            "docs/readme.md",
            ".gobby/plans/p.md",
        ] {
            let path = write_file(root, rel, b"fn fixture() {}\n");
            set_mtime(&path, base_time());
        }
        if case.indexed_excludes.is_empty() {
            assert_eq!(
                indexer_paths(root, &[]),
                [
                    ".gobby/plans/p.md",
                    "docs/readme.md",
                    "src/a.rs",
                    "src/b.rs"
                ],
                "fixture discovery"
            );
        }
        let last = watermark_after_now();
        let indexed = indexer_paths(root, &owned(case.indexed_excludes));
        (case.mutate)(root, outside.path(), last);

        let probe_excludes = owned(case.probe_excludes);
        let probe = project_changed_since(root, last, &indexed, &probe_excludes, default_options());
        let indexer = indexer_sees_change(root, last, &indexed, &probe_excludes);
        assert_eq!(probe, indexer, "{}: probe and indexer disagree", case.name);
        assert_eq!(probe, case.expected, "{}", case.name);
    }
}
