//! Inputs read from disk by the import resolver, without building its indexes.

use std::collections::BTreeSet;
use std::path::Path;

use sha2::{Digest, Sha256};

use super::package_metadata::rust_manifest_paths;

/// Covers package metadata and declaration-bearing source bytes. Other language
/// indexes depend only on candidate paths, which the community digest covers.
pub(crate) fn import_resolution_input_digest<'a>(
    root: &Path,
    visible_paths: impl IntoIterator<Item = &'a str>,
) -> String {
    let visible_paths = visible_paths.into_iter().collect::<BTreeSet<_>>();
    let mut digest = Sha256::new();
    // Go providers use canonical paths, so a symlink retarget can change the
    // import graph without changing its indexed path or import rows.
    for path in &visible_paths {
        if Path::new(path)
            .extension()
            .and_then(|extension| extension.to_str())
            == Some("go")
        {
            hash_field(&mut digest, path.as_bytes());
            match root.join(path).canonicalize() {
                Ok(canonical) => {
                    digest.update([1]);
                    hash_field(&mut digest, canonical.to_string_lossy().as_bytes());
                }
                Err(_) => digest.update([0]),
            }
        }
    }
    digest.update(b"\0declarations\0");
    let mut paths = rust_manifest_paths(root)
        .into_iter()
        .collect::<BTreeSet<_>>();
    paths.extend(
        [
            "package.json",
            "go.mod",
            "pubspec.yaml",
            "mix.exs",
            "mix.lock",
        ]
        .map(|path| root.join(path)),
    );
    paths.extend(visible_paths.into_iter().filter_map(|path| {
        let path = Path::new(path);
        matches!(
            path.extension().and_then(|extension| extension.to_str()),
            Some(
                "java"
                    | "cs"
                    | "kt"
                    | "kts"
                    | "scala"
                    | "sc"
                    | "php"
                    | "rb"
                    | "ex"
                    | "exs"
                    | "h"
                    | "m"
                    | "mm"
            )
        )
        .then(|| root.join(path))
    }));
    for path in paths {
        let relative = path.strip_prefix(root).unwrap_or(&path).to_string_lossy();
        hash_field(&mut digest, relative.as_bytes());
        match std::fs::read(&path) {
            Ok(bytes) => {
                digest.update([1]);
                hash_field(&mut digest, &bytes);
            }
            Err(_) => digest.update([0]), // resolver also ignores unreadable inputs
        }
    }
    digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn hash_field(digest: &mut Sha256, bytes: &[u8]) {
    digest.update((bytes.len() as u64).to_le_bytes());
    digest.update(bytes);
}

#[cfg(test)]
mod tests {
    use super::import_resolution_input_digest;

    #[test]
    fn resolver_digest_tracks_declarations_and_workspace_manifests() -> anyhow::Result<()> {
        let root = tempfile::tempdir()?;
        std::fs::create_dir(root.path().join("member"))?;
        std::fs::write(
            root.path().join("Cargo.toml"),
            "[workspace]\nmembers = ['member']\n",
        )?;
        std::fs::write(
            root.path().join("member/Cargo.toml"),
            "[package]\nname = 'one'\n",
        )?;
        std::fs::write(
            root.path().join("Provider.java"),
            "package one; class Provider {}",
        )?;
        let digest = || import_resolution_input_digest(root.path(), ["Provider.java"]);
        let before = digest();
        assert_eq!(digest(), before);
        std::fs::write(
            root.path().join("Provider.java"),
            "package two; class Provider {}",
        )?;
        let declarations_changed = digest();
        assert_ne!(declarations_changed, before);
        std::fs::write(
            root.path().join("member/Cargo.toml"),
            "[package]\nname = 'two'\n",
        )?;
        let member_changed = digest();
        assert_ne!(member_changed, declarations_changed);
        std::fs::write(
            root.path().join("package.json"),
            r#"{"name":"local-package"}"#,
        )?;
        assert_ne!(digest(), member_changed);
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn resolver_digest_tracks_go_symlink_targets() -> anyhow::Result<()> {
        let root = tempfile::tempdir()?;
        std::fs::create_dir(root.path().join("one"))?;
        std::fs::create_dir(root.path().join("two"))?;
        for directory in ["one", "two"] {
            std::fs::write(
                root.path().join(directory).join("source.go"),
                "package example",
            )?;
        }
        let link = root.path().join("source.go");
        std::os::unix::fs::symlink("one/source.go", &link)?;
        let before = import_resolution_input_digest(root.path(), ["source.go"]);
        std::fs::remove_file(&link)?;
        std::os::unix::fs::symlink("two/source.go", &link)?;
        assert_ne!(
            import_resolution_input_digest(root.path(), ["source.go"]),
            before
        );
        Ok(())
    }
}
