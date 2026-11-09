use super::{ExitReason, SrtInstallation, sandbox_from_inputs};
use std::{fs, path::PathBuf};

fn installation() -> SrtInstallation {
    SrtInstallation {
        root: PathBuf::new(),
        node: PathBuf::new(),
        runner: PathBuf::new(),
    }
}

#[test]
fn the_prompt_file_is_removed_when_the_operator_lookup_fails() {
    let dir = tempfile::tempdir().expect("temp dir");
    let prompt_file = dir.path().join("run.md");
    fs::write(&prompt_file, "prompt").expect("write prompt file");

    let failure = sandbox_from_inputs(
        Err(anyhow::anyhow!("passwd lookup failed")),
        &prompt_file,
        &installation(),
    )
    .expect_err("an unresolved operator account fails the launch");

    assert_eq!(failure.reason, ExitReason::PrepareFailed);
    assert!(
        !prompt_file.exists(),
        "the prompt file must not outlive a failed preparation"
    );
}
