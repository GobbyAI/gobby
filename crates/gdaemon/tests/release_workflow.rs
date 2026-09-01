use std::fs;
use std::path::Path;

#[test]
fn release_workflow_builds_publishes_and_packages_gdaemon() {
    let workflow_path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.github/workflows/release-gdaemon.yml");
    let workflow = fs::read_to_string(&workflow_path)
        .unwrap_or_else(|error| panic!("failed to read {}: {error}", workflow_path.display()));

    for required in [
        "tags:\n      - \"gdaemon-v*\"",
        "cargo clippy -p gobby-daemon --all-targets -- -D warnings",
        "cargo nextest run --profile ci -p gobby-daemon",
        "cargo publish -p gobby-daemon",
        "cargo build --release --target ${{ matrix.target }} -p gobby-daemon",
        "gh release create \"$tag\"",
        ".sha256",
    ] {
        assert!(
            workflow.contains(required),
            "release-gdaemon.yml is missing required contract: {required}"
        );
    }

    assert!(
        !workflow.contains("--no-default-features"),
        "release-gdaemon.yml must check gdaemon with its default features only"
    );

    for target in [
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
        "x86_64-pc-windows-msvc",
        "aarch64-pc-windows-msvc",
    ] {
        assert!(
            workflow.contains(target),
            "release-gdaemon.yml is missing target {target}"
        );
    }

    for action in workflow
        .lines()
        .map(str::trim)
        .filter_map(|line| line.strip_prefix("uses: "))
    {
        let (_, reference) = action
            .rsplit_once('@')
            .unwrap_or_else(|| panic!("action is unpinned: {action}"));
        assert_eq!(
            reference.len(),
            40,
            "action must use a full commit SHA: {action}"
        );
        assert!(
            reference.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "action must use a hexadecimal commit SHA: {action}"
        );
    }
}
