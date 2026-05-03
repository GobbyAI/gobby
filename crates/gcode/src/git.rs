use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorktreeKind {
    Main,
    Linked,
    NotGit,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorktreeInfo {
    pub top_level: PathBuf,
    pub git_dir: Option<PathBuf>,
    pub git_common_dir: Option<PathBuf>,
    pub kind: WorktreeKind,
}

pub fn worktree_info(path: &Path) -> anyhow::Result<WorktreeInfo> {
    let top_level = match git_output(path, &["rev-parse", "--show-toplevel"]) {
        Ok(output) => PathBuf::from(output).canonicalize()?,
        Err(_) => {
            let root = path
                .canonicalize()
                .unwrap_or_else(|_| absolute_fallback(path));
            return Ok(WorktreeInfo {
                top_level: root,
                git_dir: None,
                git_common_dir: None,
                kind: WorktreeKind::NotGit,
            });
        }
    };

    let git_dir =
        PathBuf::from(git_output(path, &["rev-parse", "--absolute-git-dir"])?).canonicalize()?;
    let common_raw = git_output(path, &["rev-parse", "--git-common-dir"])?;
    let git_common_dir = resolve_git_path(&top_level, &git_dir, &common_raw)?;
    let kind = if git_dir == git_common_dir {
        WorktreeKind::Main
    } else {
        WorktreeKind::Linked
    };

    Ok(WorktreeInfo {
        top_level,
        git_dir: Some(git_dir),
        git_common_dir: Some(git_common_dir),
        kind,
    })
}

fn git_output(path: &Path, args: &[&str]) -> anyhow::Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(path)
        .args(args)
        .output()?;
    if !output.status.success() {
        anyhow::bail!("git command failed");
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn resolve_git_path(top_level: &Path, git_dir: &Path, raw: &str) -> anyhow::Result<PathBuf> {
    let path = PathBuf::from(raw);
    if path.is_absolute() {
        return Ok(path.canonicalize()?);
    }

    let top_candidate = top_level.join(&path);
    if top_candidate.exists() {
        return Ok(top_candidate.canonicalize()?);
    }

    Ok(git_dir.join(path).canonicalize()?)
}

fn absolute_fallback(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run_git(dir: &Path, args: &[&str]) {
        let status = Command::new("git")
            .arg("-C")
            .arg(dir)
            .args(args)
            .status()
            .expect("run git");
        assert!(status.success(), "git {:?} failed", args);
    }

    fn commit_initial(repo: &Path) {
        std::fs::write(repo.join("README.md"), "hello\n").expect("write readme");
        run_git(repo, &["add", "README.md"]);
        run_git(
            repo,
            &[
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test User",
                "commit",
                "-m",
                "initial",
            ],
        );
    }

    #[test]
    fn detects_normal_repo_as_main_worktree() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir(&repo).expect("create repo");
        run_git(&repo, &["init"]);

        let info = worktree_info(&repo).expect("worktree info");

        assert_eq!(info.kind, WorktreeKind::Main);
        assert_eq!(info.top_level, repo.canonicalize().unwrap());
        assert_eq!(info.git_dir, info.git_common_dir);
    }

    #[test]
    fn detects_linked_worktree() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        let linked = tmp.path().join("linked");
        std::fs::create_dir(&repo).expect("create repo");
        run_git(&repo, &["init"]);
        commit_initial(&repo);
        run_git(
            &repo,
            &[
                "worktree",
                "add",
                "-b",
                "linked-branch",
                linked.to_str().unwrap(),
            ],
        );

        let info = worktree_info(&linked).expect("worktree info");

        assert_eq!(info.kind, WorktreeKind::Linked);
        assert_eq!(info.top_level, linked.canonicalize().unwrap());
        assert_ne!(info.git_dir, info.git_common_dir);
    }

    #[test]
    fn separate_git_dir_is_main_worktree() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        let git_dir = tmp.path().join("separate.git");
        std::fs::create_dir(&repo).expect("create repo");
        let status = Command::new("git")
            .arg("-C")
            .arg(&repo)
            .arg("init")
            .arg("--separate-git-dir")
            .arg(&git_dir)
            .status()
            .expect("run git init");
        assert!(status.success());

        let info = worktree_info(&repo).expect("worktree info");

        assert_eq!(info.kind, WorktreeKind::Main);
        assert_eq!(info.git_dir, info.git_common_dir);
        assert_eq!(info.git_dir, Some(git_dir.canonicalize().unwrap()));
    }
}
