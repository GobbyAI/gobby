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
const TAIKI_INSTALL_ACTION_SHA: &str = "f5b277aa8941a90c16bc1cd6ab9363e0502b7d31";
const ACTIONS_CHECKOUT_SHA: &str = "34e114876b0b11c390a56381ad16ebd13914f8d5";
const DTOLNAY_RUST_TOOLCHAIN_SHA: &str = "29eef336d9b2848a0b548edc03f92a220660cdb8";
const ACTIONS_CACHE_SHA: &str = "0057852bfaa89a56745cba8c7296529d2fc39830";
const ACTIONS_UPLOAD_ARTIFACT_SHA: &str = "ea165f8d65b6e75b540449e92b4886f43607fa02";

fn release_upload_marker(workflow: &str) -> Option<usize> {
    workflow
        .find("softprops/action-gh-release")
        .or_else(|| workflow.find("gh release create"))
}

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
            count_run_step(
                workflow,
                &format!("cargo nextest run --profile ci -p {package}")
            ),
            1,
            "{package} default test step count"
        );
        assert_eq!(
            count_run_step(workflow, &format!("cargo test --doc -p {package}")),
            1,
            "{package} default doctest step count"
        );
        assert_eq!(
            count_run_step(
                workflow,
                &format!("cargo nextest run --profile ci -p {package} --no-default-features")
            ),
            1,
            "{package} no-default test step count"
        );
        assert_eq!(
            count_run_step(
                workflow,
                &format!("cargo test --doc -p {package} --no-default-features")
            ),
            1,
            "{package} no-default doctest step count"
        );
    }
}

#[test]
fn release_workflows_pin_github_release_action_by_sha() {
    for (tool, workflow) in RELEASE_WORKFLOWS {
        let release_ref = format!("softprops/action-gh-release@{SOFTPROPS_ACTION_GH_RELEASE_SHA}");
        let release_uses = workflow
            .lines()
            .map(str::trim)
            .filter_map(|line| line.strip_prefix("uses: "))
            .filter(|uses| uses.starts_with("softprops/action-gh-release@"))
            .collect::<Vec<_>>();
        if !release_uses.is_empty() {
            assert!(
                release_uses
                    .iter()
                    .all(|uses| *uses == release_ref.as_str()),
                "release-{tool}.yml should pin softprops/action-gh-release by SHA"
            );
        }
    }
}

#[test]
fn ci_workflow_pins_taiki_install_actions_by_sha() {
    let workflow = include_str!("../../../.github/workflows/ci.yml");

    assert_eq!(
        workflow.matches("taiki-e/install-action@nextest").count(),
        0
    );
    assert_eq!(
        workflow
            .matches("taiki-e/install-action@cargo-llvm-cov")
            .count(),
        0
    );
    assert_eq!(
        workflow
            .matches(&format!(
                "taiki-e/install-action@{TAIKI_INSTALL_ACTION_SHA}"
            ))
            .count(),
        3
    );
    assert!(workflow.contains("tool: nextest"));
    assert!(workflow.contains("tool: cargo-llvm-cov"));
}

#[test]
fn ci_workflow_pins_core_actions_by_sha() {
    let workflow = include_str!("../../../.github/workflows/ci.yml");

    assert_eq!(
        workflow
            .matches(&format!("actions/checkout@{ACTIONS_CHECKOUT_SHA}"))
            .count(),
        2
    );
    assert_eq!(workflow.matches("actions/checkout@v4").count(), 0);
    assert_eq!(workflow.matches("persist-credentials: false").count(), 2);

    assert_eq!(
        workflow
            .matches(&format!(
                "dtolnay/rust-toolchain@{DTOLNAY_RUST_TOOLCHAIN_SHA}"
            ))
            .count(),
        2
    );
    assert_eq!(workflow.matches("dtolnay/rust-toolchain@stable").count(), 0);
    assert_eq!(workflow.matches("toolchain: stable").count(), 2);

    assert_eq!(
        workflow
            .matches(&format!("actions/cache@{ACTIONS_CACHE_SHA}"))
            .count(),
        2
    );
    assert_eq!(workflow.matches("actions/cache@v4").count(), 0);

    assert_eq!(
        workflow
            .matches(&format!(
                "actions/upload-artifact@{ACTIONS_UPLOAD_ARTIFACT_SHA}"
            ))
            .count(),
        1
    );
    assert_eq!(workflow.matches("actions/upload-artifact@v4").count(), 0);
}

#[test]
fn release_gwiki_uses_github_cli_release_creation() {
    let workflow = include_str!("../../../.github/workflows/release-gwiki.yml");

    assert!(
        !workflow.contains("softprops/action-gh-release"),
        "release-gwiki.yml should use the GitHub CLI instead of softprops"
    );
    assert!(
        workflow.contains("GH_TOKEN: ${{ github.token }}"),
        "release-gwiki.yml should authenticate gh with github.token"
    );
    assert!(
        workflow
            .contains(r#"gh release create "$tag" "${assets[@]}" --generate-notes --verify-tag"#),
        "release-gwiki.yml should create the release with generated notes and tag verification"
    );
}

#[test]
fn release_gsqz_uses_github_cli_release_creation() {
    let workflow = include_str!("../../../.github/workflows/release-gsqz.yml");

    assert!(
        !workflow.contains("softprops/action-gh-release"),
        "release-gsqz.yml should use the GitHub CLI instead of softprops"
    );
    assert!(
        workflow.contains("GITHUB_RELEASE_TOKEN: ${{ github.token }}"),
        "release-gsqz.yml should pass github.token to the explicit gh auth flow"
    );
    assert!(
        workflow.contains("gh auth login --with-token"),
        "release-gsqz.yml should authenticate gh explicitly"
    );
    assert!(
        workflow
            .contains(r#"gh release create "$tag" "${assets[@]}" --generate-notes --verify-tag"#),
        "release-gsqz.yml should create the release with generated notes and tag verification"
    );
}

#[test]
fn release_gloc_uses_github_cli_release_creation_and_upload() {
    let workflow = include_str!("../../../.github/workflows/release-gloc.yml");

    assert!(
        !workflow.contains("softprops/action-gh-release"),
        "release-gloc.yml should use the GitHub CLI instead of softprops"
    );
    assert!(
        workflow.contains("GH_TOKEN: ${{ github.token }}"),
        "release-gloc.yml should authenticate gh with github.token"
    );
    assert!(
        workflow.contains(r#"gh release create "$tag" --generate-notes --verify-tag"#),
        "release-gloc.yml should create generated release notes with tag verification"
    );
    assert!(
        workflow.contains(r#"gh release upload "$tag" "${assets[@]}""#),
        "release-gloc.yml should upload all generated gloc assets"
    );
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
        let release_upload = release_upload_marker(workflow)
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

        if matches!(tool, "gwiki" | "gsqz") {
            assert!(
                workflow[checksum_step..].contains(&format!(
                    "assets=({tool}-*.tar.gz {tool}-*.zip {tool}-*.sha256)"
                )),
                "release-{tool}.yml should add checksum files to the gh release asset array"
            );
        } else if tool == "gloc" {
            assert!(
                workflow[checksum_step..]
                    .contains("assets=(gloc-*.tar.gz gloc-*.zip gloc-*.sha256)"),
                "release-gloc.yml should add checksum files to the gh release asset array"
            );
            assert!(
                workflow[release_upload..].contains(r#"gh release upload "$tag" "${assets[@]}""#),
                "release-gloc.yml should upload checksum files with gh release upload"
            );
        } else {
            let release_body = &workflow[release_upload..];
            assert!(
                release_body.contains(&format!("{tool}-*.sha256")),
                "release-{tool}.yml should upload checksum files"
            );
        }
    }
}
