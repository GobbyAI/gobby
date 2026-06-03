fn count_run_step(workflow: &str, command: &str) -> usize {
    workflow
        .lines()
        .filter(|line| line.trim() == format!("run: {command}"))
        .count()
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
