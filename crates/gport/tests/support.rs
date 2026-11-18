//! Shared helpers: run the built `gport`, and build a temp git repository.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use tempfile::TempDir;

/// The outcome of one `gport` run.
pub struct Run {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

fn finish(output: Output) -> Run {
    Run {
        code: output.status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
    }
}

/// Run `gport <args>` with `dir` as its working directory.
pub fn gport(dir: &Path, args: &[&str]) -> Run {
    gport_env(dir, args, &[])
}

/// Run `gport <args>` in `dir` with extra environment variables.
pub fn gport_env(dir: &Path, args: &[&str], env: &[(&str, &str)]) -> Run {
    let mut command = Command::new(env!("CARGO_BIN_EXE_gport"));
    command.current_dir(dir).args(args);
    for (name, value) in env {
        command.env(name, value);
    }
    finish(command.output().expect("gport runs"))
}

/// The real repository root, for tests that read the committed map and ledger.
pub fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("repository root")
}

/// A temp git repository whose first branch is `0.9.0`.
pub struct Repo {
    dir: TempDir,
}

impl Repo {
    pub fn new() -> Self {
        let repo = Self {
            dir: TempDir::new().expect("temp dir"),
        };
        repo.git(&["init", "-q", "-b", "0.9.0"]);
        repo.git(&["config", "user.email", "test@example.invalid"]);
        repo.git(&["config", "user.name", "Test"]);
        repo.git(&["config", "commit.gpgsign", "false"]);
        repo
    }

    pub fn path(&self) -> &Path {
        self.dir.path()
    }

    /// Run git and return trimmed stdout; panics when git fails.
    pub fn git(&self, args: &[&str]) -> String {
        let output = Command::new("git")
            .current_dir(self.path())
            .args(args)
            .output()
            .expect("git runs");
        assert!(
            output.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8_lossy(&output.stdout).trim().to_owned()
    }

    /// Write a file, creating its parent directories.
    pub fn write(&self, path: &str, content: &str) {
        let full = self.path().join(path);
        std::fs::create_dir_all(full.parent().expect("parent")).expect("create dirs");
        std::fs::write(full, content).expect("write file");
    }

    /// Stage everything and commit; returns the new commit's full SHA.
    pub fn commit(&self, subject: &str) -> String {
        self.git(&["add", "-A"]);
        self.git(&["commit", "-q", "--allow-empty", "-m", subject]);
        self.git(&["rev-parse", "HEAD"])
    }
}
