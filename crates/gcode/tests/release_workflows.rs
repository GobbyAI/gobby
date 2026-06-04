fn count_run_step(workflow: &str, command: &str) -> usize {
    workflow
        .lines()
        .filter(|line| line.trim() == format!("run: {command}"))
        .count()
}

const RELEASE_WORKFLOWS: [(&str, &str); 5] = [
    (
        "gcode",
        include_str!("../../../.github/workflows/release-gcode.yml"),
    ),
    (
        "ghook",
        include_str!("../../../.github/workflows/release-ghook.yml"),
    ),
    (
        "gloc",
        include_str!("../../../.github/workflows/release-gloc.yml"),
    ),
    (
        "gsqz",
        include_str!("../../../.github/workflows/release-gsqz.yml"),
    ),
    (
        "gwiki",
        include_str!("../../../.github/workflows/release-gwiki.yml"),
    ),
];

const SOFTPROPS_ACTION_GH_RELEASE_SHA: &str = "3bb12739c298aeb8a4eeaf626c5b8d85266b0e65";

#[test]
fn release_workflows_have_one_default_and_one_no_default_check() {
    let cases = [
        (
            include_str!("../../../.github/workflows/release-gcode.yml"),
            "gobby-code",
        ),
        (
            include_str!("../../../.github/workflows/release-gsqz.yml"),
            "gobby-squeeze",
        ),
    ];

    for (workflow, package) in cases {
        assert_eq!(
            count_run_step(
                workflow,
                &format!("cargo clippy -p {package} -- -D warnings")
            ),
            1,
            "{package} default clippy step count"
        );
        assert_eq!(
            count_run_step(
                workflow,
                &format!("cargo clippy -p {package} --no-default-features -- -D warnings")
            ),
            1,
            "{package} no-default clippy step count"
        );
        assert_eq!(
            count_run_step(workflow, &format!("cargo test -p {package}")),
            1,
            "{package} default test step count"
        );
        assert_eq!(
            count_run_step(
                workflow,
                &format!("cargo test -p {package} --no-default-features")
            ),
            1,
            "{package} no-default test step count"
        );
    }
}

#[test]
fn release_workflows_pin_github_release_action_by_sha() {
    for (tool, workflow) in RELEASE_WORKFLOWS {
        let release_ref = format!("softprops/action-gh-release@{SOFTPROPS_ACTION_GH_RELEASE_SHA}");
        if workflow.contains("softprops/action-gh-release") {
            assert!(
                workflow.contains(&release_ref),
                "release-{tool}.yml should pin softprops/action-gh-release by SHA"
            );
            assert!(
                !workflow.contains("softprops/action-gh-release@v2"),
                "release-{tool}.yml should not use a mutable softprops/action-gh-release tag"
            );
        }
    }
}

#[test]
fn release_gwiki_validates_dependencies_without_python_helper() {
    let workflow = include_str!("../../../.github/workflows/release-gwiki.yml");

    assert!(
        !workflow.contains("python3"),
        "release-gwiki.yml should validate dependencies inline without python3"
    );
    assert!(
        !workflow.contains(".github/scripts/verify-gwiki-deps.py"),
        "release-gwiki.yml should not reference the removed dependency helper"
    );
}

#[test]
fn release_workflows_generate_and_upload_archive_checksums() {
    for (tool, workflow) in RELEASE_WORKFLOWS {
        let checksum_step = workflow
            .find("name: Generate SHA-256 checksums")
            .unwrap_or_else(|| panic!("release-{tool}.yml missing checksum generation step"));
        let release_upload = workflow
            .find("softprops/action-gh-release")
            .unwrap_or_else(|| panic!("release-{tool}.yml missing GitHub release upload"));

        assert!(
            checksum_step < release_upload,
            "release-{tool}.yml should generate checksums before release upload"
        );

        let checksum_body = &workflow[checksum_step..release_upload];
        assert!(
            checksum_body.contains(&format!("{tool}-*.tar.gz")),
            "release-{tool}.yml should generate tar.gz checksums"
        );
        assert!(
            checksum_body.contains(&format!("{tool}-*.zip")),
            "release-{tool}.yml should generate zip checksums"
        );
        assert!(
            checksum_body.contains(r#"sha256sum "$asset" > "$asset.sha256""#),
            "release-{tool}.yml should write per-asset checksum files"
        );

        let release_body = &workflow[release_upload..];
        assert!(
            release_body.contains(&format!("{tool}-*.sha256")),
            "release-{tool}.yml should upload checksum files"
        );
    }
}
