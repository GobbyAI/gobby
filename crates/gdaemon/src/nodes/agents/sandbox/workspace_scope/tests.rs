use super::{WorkspaceScope, checked_scope, paths, within};
use anyhow::Result;
use std::{fs, os::unix::fs::symlink, path::Path};

#[test]
fn read_grants_exclude_foreign_aliases_and_globs() -> Result<()> {
    let root = Path::new("/home/agents_workspaces").to_path_buf();
    let own = root.join("project/developer/session-7");
    let scope = WorkspaceScope {
        root,
        reads: vec![own.clone()],
        writes: vec![own.clone()],
    };
    let reads = [
        "/home/agents_workspaces",
        "/home/agents_workspaces/foreign",
        "/home",
        "/HOME/AGENTS_WORKSPACES/foreign",
        "/home/agents_workspaces/project/*",
        "/outside",
    ]
    .map(str::to_owned)
    .to_vec();
    assert_eq!(
        scope.restrict(reads, &[paths::text(&own)])?,
        vec!["/outside".to_owned(), paths::text(&own)]
    );
    assert!(within(
        Path::new("/Straße/AGENTS_WORKSPACES/x"),
        Path::new("/STRASSE/agents_workspaces")
    ));
    Ok(())
}

#[test]
fn read_all_does_not_authorize_workspace_writes() -> Result<()> {
    let root = Path::new("/home/agents_workspaces").to_path_buf();
    let own = root.join("project/developer/session-7");
    let scope = WorkspaceScope {
        root: root.clone(),
        reads: vec![root.clone()],
        writes: vec![own.clone()],
    };
    assert_eq!(
        scope.restrict(Vec::new(), &[paths::text(&own)])?,
        vec![paths::text(&root)]
    );
    for denied in [
        root,
        own.parent().unwrap().to_path_buf(),
        own.with_file_name("session-8"),
    ] {
        let error = scope
            .restrict(Vec::new(), &[paths::text(&denied)])
            .unwrap_err();
        assert!(
            error
                .to_string()
                .contains("write grant exceeds agent workspace scope")
        );
    }
    Ok(())
}

#[test]
fn concrete_scope_refuses_globs_and_symlink_redirection() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?.join("agents_workspaces");
    fs::create_dir(&root)?;
    let checked = checked_scope(vec![root.clone()], &root, &root)?;
    assert_eq!(paths::text(&checked[0]), paths::text(&root));
    for component in ["*", "?", "[ab]", "{a,b}"] {
        let error = checked_scope(vec![root.join(component)], &root, &root).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("workspace scope contains glob syntax")
        );
    }
    let foreign = root.join("foreign");
    fs::create_dir(&foreign)?;
    let own = root.join("own");
    symlink(&foreign, &own)?;
    let error = checked_scope(vec![own], &root, &root).unwrap_err();
    assert!(
        error
            .to_string()
            .contains("workspace scope follows an alias")
    );
    Ok(())
}
