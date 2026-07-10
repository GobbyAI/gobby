use std::fs;
use std::io::Read as _;
use std::path::{Path, PathBuf};

use serde_json::json;

use super::paths::{PathViolation, normalize_requested_path};
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, PageWriteMode, ScopeIdentity, ScopeSelection, WikiError};

/// The only vault prefix `gwiki page` may mutate; `code/**`, `outputs/**`,
/// `raw/**`, `meta/**`, and `.obsidian/**` stay generated/derived surfaces.
const WRITABLE_PREFIX: &str = "knowledge";

pub(crate) fn execute_write(
    selection: ScopeSelection,
    path: String,
    mode: PageWriteMode,
    expected_hash: Option<String>,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let content = read_stdin_content()?;
    write_page(
        scope.root(),
        &output_scope,
        &path,
        mode,
        expected_hash.as_deref(),
        &content,
    )
}

pub(crate) fn execute_delete(
    selection: ScopeSelection,
    path: String,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    delete_page(scope.root(), &output_scope, &path)
}

fn write_page(
    root: &Path,
    scope: &ScopeIdentity,
    requested: &str,
    mode: PageWriteMode,
    expected_hash: Option<&str>,
    content: &str,
) -> Result<CommandOutcome, WikiError> {
    let confined = confine_page_path(root, requested)?;
    let rel = confined.display_path();
    let exists = confined.absolute.exists();

    match mode {
        PageWriteMode::Create => {
            if expected_hash.is_some() {
                return Err(WikiError::InvalidInput {
                    field: "--expected-hash",
                    message: "--expected-hash requires --mode upsert; create's precondition \
                              is that the page does not exist"
                        .to_string(),
                });
            }
            if exists {
                return Err(WikiError::AlreadyExists {
                    resource: "wiki page",
                    id: rel,
                });
            }
        }
        PageWriteMode::Upsert => {
            if let Some(expected) = expected_hash {
                if !exists {
                    return Err(WikiError::PreconditionFailed {
                        detail: format!(
                            "expected content hash {expected}, but wiki page `{rel}` does \
                             not exist"
                        ),
                    });
                }
                let actual = gobby_core::indexing::file_content_hash(&confined.absolute).map_err(
                    |source| WikiError::Io {
                        action: "hash existing wiki page",
                        path: Some(confined.absolute.clone()),
                        source,
                    },
                )?;
                if !actual.eq_ignore_ascii_case(expected) {
                    return Err(WikiError::PreconditionFailed {
                        detail: format!(
                            "expected content hash {expected}, found {actual} for wiki \
                             page `{rel}`"
                        ),
                    });
                }
            }
        }
    }

    if let Some(parent) = confined.absolute.parent() {
        fs::create_dir_all(parent).map_err(|source| WikiError::Io {
            action: "create wiki page parent directories",
            path: Some(parent.to_path_buf()),
            source,
        })?;
    }
    // Re-verify after directory creation so a symlink racing into the chain
    // cannot redirect the write.
    verify_ancestor_confinement(root, &confined.absolute)?;
    fs::write(&confined.absolute, content).map_err(|source| WikiError::Io {
        action: "write wiki page",
        path: Some(confined.absolute.clone()),
        source,
    })?;

    let text = format!(
        "Wrote wiki page {rel} ({} bytes)\nScope: {scope}",
        content.len()
    );
    let payload = json!({
        "command": "page-write",
        "scope": scope,
        "path": rel,
        "created": !exists,
        "bytes": content.len(),
        "content_hash": gobby_core::indexing::content_hash(content.as_bytes()),
        "changed_paths": [rel],
    });
    Ok(super::scoped_outcome("page-write", scope, payload, text))
}

fn delete_page(
    root: &Path,
    scope: &ScopeIdentity,
    requested: &str,
) -> Result<CommandOutcome, WikiError> {
    let confined = confine_page_path(root, requested)?;
    let rel = confined.display_path();
    if !confined.absolute.exists() {
        return Err(WikiError::NotFound {
            resource: "wiki page",
            id: rel,
        });
    }
    fs::remove_file(&confined.absolute).map_err(|source| WikiError::Io {
        action: "delete wiki page",
        path: Some(confined.absolute.clone()),
        source,
    })?;

    let text = format!("Deleted wiki page {rel}\nScope: {scope}");
    let payload = json!({
        "command": "page-delete",
        "scope": scope,
        "path": rel,
        // The incremental indexer's IndexEvent::Deleted path prunes the
        // derived DB rows when this path is reindexed.
        "changed_paths": [rel],
    });
    Ok(super::scoped_outcome("page-delete", scope, payload, text))
}

struct ConfinedPage {
    relative: PathBuf,
    absolute: PathBuf,
}

impl ConfinedPage {
    fn display_path(&self) -> String {
        self.relative.to_string_lossy().replace('\\', "/")
    }
}

fn confine_page_path(root: &Path, requested: &str) -> Result<ConfinedPage, WikiError> {
    let relative = normalize_requested_path(Path::new(requested)).map_err(|violation| {
        WikiError::InvalidInput {
            field: "path",
            message: match violation {
                PathViolation::Absolute => {
                    "page paths must be vault-relative, not absolute".to_string()
                }
                PathViolation::Escape => {
                    "page paths must stay inside the selected wiki scope".to_string()
                }
                PathViolation::Empty => "page path must identify a wiki page".to_string(),
            },
        }
    })?;

    if relative.extension().and_then(|value| value.to_str()) != Some("md") {
        return Err(WikiError::InvalidInput {
            field: "path",
            message: "page paths must name a markdown file ending in .md".to_string(),
        });
    }

    let mut components = relative
        .components()
        .filter_map(|component| component.as_os_str().to_str());
    if components.next() != Some(WRITABLE_PREFIX) || components.next().is_none() {
        return Err(WikiError::InvalidInput {
            field: "path",
            message: format!("page writes and deletes are confined to {WRITABLE_PREFIX}/**"),
        });
    }

    let absolute = root.join(&relative);
    if let Ok(metadata) = fs::symlink_metadata(&absolute)
        && metadata.file_type().is_symlink()
    {
        return Err(WikiError::InvalidInput {
            field: "path",
            message: "page path resolves to a symlink; refusing to write through it".to_string(),
        });
    }
    verify_ancestor_confinement(root, &absolute)?;

    Ok(ConfinedPage { relative, absolute })
}

/// Canonicalize the deepest existing ancestor of `absolute` and require that
/// it resolves to the same vault-relative location it names lexically. Any
/// symlinked component — escaping the vault or redirecting inside it — makes
/// the canonical and lexical relatives diverge and is rejected.
fn verify_ancestor_confinement(root: &Path, absolute: &Path) -> Result<(), WikiError> {
    let canonical_root = root.canonicalize().map_err(|source| WikiError::Io {
        action: "canonicalize wiki vault root",
        path: Some(root.to_path_buf()),
        source,
    })?;

    let mut ancestor = absolute.parent().unwrap_or(root);
    while !ancestor.exists() {
        ancestor = match ancestor.parent() {
            Some(parent) => parent,
            None => break,
        };
    }

    let escape = || WikiError::InvalidInput {
        field: "path",
        message: "page path escapes the wiki vault via a symlink".to_string(),
    };
    let canonical_ancestor = ancestor.canonicalize().map_err(|_| escape())?;
    let canonical_relative = canonical_ancestor
        .strip_prefix(&canonical_root)
        .map_err(|_| escape())?;
    let lexical_relative = ancestor.strip_prefix(root).map_err(|_| escape())?;
    if canonical_relative != lexical_relative {
        return Err(escape());
    }
    Ok(())
}

fn read_stdin_content() -> Result<String, WikiError> {
    let mut content = String::new();
    std::io::stdin()
        .lock()
        .read_to_string(&mut content)
        .map_err(|source| WikiError::Io {
            action: "read page content from stdin",
            path: None,
            source,
        })?;
    Ok(content)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::Path;

    use serde_json::json;

    use super::*;

    fn scope() -> ScopeIdentity {
        ScopeIdentity::project("test-project")
    }

    fn write(
        root: &Path,
        requested: &str,
        mode: PageWriteMode,
        expected_hash: Option<&str>,
        content: &str,
    ) -> Result<CommandOutcome, WikiError> {
        write_page(root, &scope(), requested, mode, expected_hash, content)
    }

    #[test]
    fn write_upserts_knowledge_page_from_stdin_content() {
        let temp = tempfile::tempdir().expect("tempdir");
        let content = "---\ntitle: Example\n---\n# Example\n";

        let outcome = write(
            temp.path(),
            "knowledge/topics/example.md",
            PageWriteMode::Upsert,
            None,
            content,
        )
        .expect("first upsert creates the page");

        let payload = &outcome.result.payload;
        assert_eq!(payload["command"], "page-write");
        assert_eq!(payload["path"], "knowledge/topics/example.md");
        assert_eq!(payload["created"], true);
        assert_eq!(payload["bytes"], content.len());
        assert_eq!(
            payload["content_hash"],
            gobby_core::indexing::content_hash(content.as_bytes())
        );
        assert_eq!(
            payload["changed_paths"],
            json!(["knowledge/topics/example.md"])
        );
        let on_disk = fs::read_to_string(temp.path().join("knowledge/topics/example.md"))
            .expect("page written");
        assert_eq!(on_disk, content, "frontmatter round-trips verbatim");

        let updated = "# Replaced\n";
        let outcome = write(
            temp.path(),
            "knowledge/topics/example.md",
            PageWriteMode::Upsert,
            None,
            updated,
        )
        .expect("second upsert overwrites");
        assert_eq!(outcome.result.payload["created"], false);
        assert_eq!(
            fs::read_to_string(temp.path().join("knowledge/topics/example.md"))
                .expect("page rewritten"),
            updated
        );
    }

    #[test]
    fn write_rejects_confinement_violations() {
        let temp = tempfile::tempdir().expect("tempdir");
        fs::create_dir_all(temp.path().join("knowledge")).expect("knowledge dir");

        let rejected = [
            "/abs/knowledge/a.md",
            "knowledge/../outputs/a.md",
            "code/concepts/a.md",
            "outputs/report.md",
            "raw/source.md",
            "meta/health/snapshot.md",
            ".obsidian/workspace.md",
            "knowledge/topics/note.txt",
            "knowledge",
            "knowledge.md",
            ".",
        ];
        for requested in rejected {
            let error = write(temp.path(), requested, PageWriteMode::Upsert, None, "# x\n")
                .expect_err(&format!("{requested} must be rejected"));
            assert_eq!(error.code(), "invalid_input", "{requested}");
        }
        assert!(
            !temp.path().join("outputs").exists(),
            "no rejected write may materialize outside knowledge/"
        );
    }

    #[cfg(unix)]
    #[test]
    fn write_rejects_symlink_escapes() {
        let outside = tempfile::tempdir().expect("outside tempdir");
        let temp = tempfile::tempdir().expect("vault tempdir");
        fs::create_dir_all(temp.path().join("knowledge")).expect("knowledge dir");

        // Directory symlink pointing out of the vault.
        std::os::unix::fs::symlink(outside.path(), temp.path().join("knowledge/linked"))
            .expect("dir symlink");
        let error = write(
            temp.path(),
            "knowledge/linked/escape.md",
            PageWriteMode::Upsert,
            None,
            "# escape\n",
        )
        .expect_err("dir symlink escape rejected");
        assert_eq!(error.code(), "invalid_input");
        assert!(
            !outside.path().join("escape.md").exists(),
            "nothing may be written through the symlink"
        );

        // File symlink pointing out of the vault.
        let target = outside.path().join("target.md");
        fs::write(&target, "outside content").expect("outside file");
        std::os::unix::fs::symlink(&target, temp.path().join("knowledge/link.md"))
            .expect("file symlink");
        let error = write(
            temp.path(),
            "knowledge/link.md",
            PageWriteMode::Upsert,
            None,
            "# overwrite\n",
        )
        .expect_err("file symlink target rejected");
        assert_eq!(error.code(), "invalid_input");
        assert_eq!(
            fs::read_to_string(&target).expect("outside file intact"),
            "outside content"
        );

        // knowledge/ itself symlinked to another vault directory.
        let redirected = tempfile::tempdir().expect("redirect tempdir");
        fs::create_dir_all(redirected.path().join("code")).expect("code dir");
        std::os::unix::fs::symlink(
            redirected.path().join("code"),
            redirected.path().join("knowledge"),
        )
        .expect("knowledge symlink");
        let error = write(
            redirected.path(),
            "knowledge/redirected.md",
            PageWriteMode::Upsert,
            None,
            "# redirected\n",
        )
        .expect_err("knowledge redirect rejected");
        assert_eq!(error.code(), "invalid_input");
        assert!(!redirected.path().join("code/redirected.md").exists());
    }

    #[test]
    fn create_mode_conflicts_on_existing() {
        let temp = tempfile::tempdir().expect("tempdir");
        let original = "# Original\n";
        write(
            temp.path(),
            "knowledge/topics/existing.md",
            PageWriteMode::Create,
            None,
            original,
        )
        .expect("create writes a new page");

        let error = write(
            temp.path(),
            "knowledge/topics/existing.md",
            PageWriteMode::Create,
            None,
            "# Clobber\n",
        )
        .expect_err("create on existing page conflicts");
        assert_eq!(error.code(), "already_exists");
        assert_eq!(
            fs::read_to_string(temp.path().join("knowledge/topics/existing.md"))
                .expect("page intact"),
            original
        );
    }

    #[test]
    fn write_precondition_hash_mismatch() {
        let temp = tempfile::tempdir().expect("tempdir");
        let original = "# Original\n";
        write(
            temp.path(),
            "knowledge/topics/guarded.md",
            PageWriteMode::Upsert,
            None,
            original,
        )
        .expect("seed page");
        let original_hash = gobby_core::indexing::content_hash(original.as_bytes());

        // Mismatched hash: distinct error, file untouched.
        let stale_hash = gobby_core::indexing::content_hash(b"some other revision");
        let error = write(
            temp.path(),
            "knowledge/topics/guarded.md",
            PageWriteMode::Upsert,
            Some(&stale_hash),
            "# Lost update\n",
        )
        .expect_err("stale hash must fail the precondition");
        assert_eq!(error.code(), "precondition_failed");
        assert_eq!(
            fs::read_to_string(temp.path().join("knowledge/topics/guarded.md"))
                .expect("page intact"),
            original
        );

        // Missing page with an expected hash is also a precondition failure.
        let error = write(
            temp.path(),
            "knowledge/topics/absent.md",
            PageWriteMode::Upsert,
            Some(&original_hash),
            "# Absent\n",
        )
        .expect_err("expected hash against a missing page fails");
        assert_eq!(error.code(), "precondition_failed");
        assert!(!temp.path().join("knowledge/topics/absent.md").exists());

        // --expected-hash only composes with upsert.
        let error = write(
            temp.path(),
            "knowledge/topics/new.md",
            PageWriteMode::Create,
            Some(&original_hash),
            "# New\n",
        )
        .expect_err("create mode rejects --expected-hash");
        assert_eq!(error.code(), "invalid_input");

        // Matching hash (any hex case) writes through.
        let outcome = write(
            temp.path(),
            "knowledge/topics/guarded.md",
            PageWriteMode::Upsert,
            Some(&original_hash.to_uppercase()),
            "# Revised\n",
        )
        .expect("matching hash writes");
        assert_eq!(outcome.result.payload["created"], false);
        assert_eq!(
            fs::read_to_string(temp.path().join("knowledge/topics/guarded.md"))
                .expect("page revised"),
            "# Revised\n"
        );
    }

    #[test]
    fn delete_removes_page_and_emits_changed_paths_for_reindex_prune() {
        let temp = tempfile::tempdir().expect("tempdir");
        write(
            temp.path(),
            "knowledge/topics/doomed.md",
            PageWriteMode::Upsert,
            None,
            "# Doomed\n",
        )
        .expect("seed page");

        let outcome = delete_page(temp.path(), &scope(), "knowledge/topics/doomed.md")
            .expect("delete removes the page");
        let payload = &outcome.result.payload;
        assert_eq!(payload["command"], "page-delete");
        assert_eq!(payload["path"], "knowledge/topics/doomed.md");
        // changed_paths drives reindex: the incremental indexer's
        // IndexEvent::Deleted path prunes the derived DB rows.
        assert_eq!(
            payload["changed_paths"],
            json!(["knowledge/topics/doomed.md"])
        );
        assert!(!temp.path().join("knowledge/topics/doomed.md").exists());

        let error = delete_page(temp.path(), &scope(), "knowledge/topics/doomed.md")
            .expect_err("deleting a missing page errors");
        assert_eq!(error.code(), "not_found");

        // Delete shares write confinement.
        let error = delete_page(temp.path(), &scope(), "code/concepts/generated.md")
            .expect_err("delete outside knowledge/ rejected");
        assert_eq!(error.code(), "invalid_input");
    }
}
