use super::super::UnsupportedFileType;
use super::super::lifecycle::current_file_state;
use super::fixtures::write_file;

#[test]
fn current_file_state_keeps_unhashable_paths_present() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_file(temp.path(), "src/lib.rs", b"fn main() {}\n");
    std::fs::create_dir_all(temp.path().join("src/unreadable")).expect("create directory");

    let state = current_file_state(
        temp.path(),
        &[
            temp.path().join("src/lib.rs"),
            temp.path().join("src/unreadable"),
        ],
        &[],
    );

    assert!(state.present_paths.contains("src/lib.rs"));
    assert!(state.present_paths.contains("src/unreadable"));
    assert!(state.hashes.contains_key("src/lib.rs"));
    assert!(!state.hashes.contains_key("src/unreadable"));
}

#[test]
fn unsupported_file_types_group_content_only_paths() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_file(temp.path(), "notes.txt", b"plain notes\n");
    write_file(temp.path(), "docs/tasks.TXT", b"more notes\n");
    write_file(temp.path(), "README.md", b"# Project\n");
    write_file(temp.path(), "docs/reference.markdown", b"# Reference\n");
    write_file(temp.path(), "Dockerfile", b"FROM rust:latest\n");

    let unsupported = super::super::util::unsupported_file_types(
        temp.path(),
        &[
            temp.path().join("notes.txt"),
            temp.path().join("docs/tasks.TXT"),
            temp.path().join("README.md"),
            temp.path().join("docs/reference.markdown"),
            temp.path().join("Dockerfile"),
        ],
    );

    assert_eq!(
        unsupported,
        vec![
            UnsupportedFileType {
                extension: ".markdown".to_string(),
                files: 1,
                examples: vec!["docs/reference.markdown".to_string()],
            },
            UnsupportedFileType {
                extension: ".md".to_string(),
                files: 1,
                examples: vec!["README.md".to_string()],
            },
            UnsupportedFileType {
                extension: ".txt".to_string(),
                files: 2,
                examples: vec!["notes.txt".to_string(), "docs/tasks.TXT".to_string()],
            },
            UnsupportedFileType {
                extension: "extensionless".to_string(),
                files: 1,
                examples: vec!["Dockerfile".to_string()],
            },
        ]
    );
}
