//! Security checks for code indexing.
//! Ports logic from src/gobby/code_index/security.py.

use std::path::Path;

const SECRET_EXTENSIONS: &[&str] = &[
    ".env",
    ".pem",
    ".key",
    ".crt",
    ".cer",
    ".der",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".secret",
];

const PRIVATE_KEY_NAMES: &[&str] = &["id_rsa", "id_ed25519"];

const PLAINTEXT_SECRET_EXTENSIONS: &[&str] = &[
    "",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".conf",
    ".properties",
    ".xml",
];

/// Generated output directories that are excluded only when they are the
/// first component under the indexed root.
const ROOT_GENERATED_DIRS: &[&str] = &["build", "dist"];

/// Check that `path` resolves within `root` (prevents directory traversal).
pub fn validate_path(path: &Path, root: &Path) -> bool {
    match (path.canonicalize(), root.canonicalize()) {
        (Ok(resolved), Ok(root_resolved)) => resolved.starts_with(&root_resolved),
        _ => false,
    }
}

/// Check that a symlink target is still within root.
pub fn is_symlink_safe(path: &Path, root: &Path) -> bool {
    if !path.is_symlink() {
        return true;
    }
    validate_path(path, root)
}

/// Check if file appears to be binary (has null bytes anywhere in the stream).
pub fn is_binary(path: &Path) -> bool {
    use std::io::Read;
    let mut file = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return true,
    };
    // Scan the whole stream, not just the first 8KB: NUL bytes can appear late
    // (a clean prefix followed by binary garbage corrupts the index — gobby-cli
    // #17356 / Gobby #17344). The read is bounded: `is_safe_text_file` already
    // rejects files larger than MAX_FILE_SIZE before this runs, so the loop reads
    // at most that cap. Do not narrow this back to a single read.
    let mut buf = [0u8; 8192];
    loop {
        let n = match file.read(&mut buf) {
            Ok(n) => n,
            Err(_) => return true,
        };
        if n == 0 {
            return false;
        }
        if buf[..n].contains(&0) {
            return true;
        }
    }
}

/// Check if a path should be excluded.
///
/// Patterns listed in `ROOT_GENERATED_DIRS` match only the first relative path
/// component, so source paths like `src/package/build/mod.rs` remain indexable.
/// Other exclude patterns match any component of the relative path.
/// Root-generated directory names are literal component names; wildcard
/// patterns such as `build*` do not get root-only special handling.
pub fn should_exclude_path(root: &Path, path: &Path, patterns: &[impl AsRef<str>]) -> bool {
    let rel = path.strip_prefix(root).unwrap_or(path);

    for pattern in patterns {
        let pattern = pattern.as_ref();
        if is_root_generated_dir(pattern) {
            if rel
                .components()
                .next()
                .map(|component| glob_match(pattern, &component.as_os_str().to_string_lossy()))
                .unwrap_or(false)
            {
                return true;
            }
            continue;
        }

        for component in rel.components() {
            let name = component.as_os_str().to_string_lossy();
            if glob_match(pattern, &name) {
                return true;
            }
        }
    }

    false
}

fn is_root_generated_dir(pattern: &str) -> bool {
    ROOT_GENERATED_DIRS.contains(&pattern)
}

/// Check if a filename suggests secret content.
pub fn has_secret_extension(path: &Path) -> bool {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    let suffix = path
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()))
        .unwrap_or_default();

    if SECRET_EXTENSIONS.contains(&suffix.as_str()) {
        return true;
    }
    if name.starts_with(".env") {
        return true;
    }
    if PRIVATE_KEY_NAMES.iter().any(|private_name| {
        name == *private_name
            || name
                .strip_prefix(*private_name)
                .is_some_and(|rest| rest.starts_with('.'))
    }) {
        return true;
    }

    let plaintext_name = name.strip_prefix('.').unwrap_or(name.as_str());
    let plaintext_stem = if suffix.is_empty() {
        plaintext_name
    } else {
        plaintext_name
            .strip_suffix(&suffix)
            .unwrap_or(plaintext_name)
    };

    PLAINTEXT_SECRET_EXTENSIONS.contains(&suffix.as_str())
        && is_plaintext_secret_name(plaintext_stem)
}

fn is_plaintext_secret_name(stem: &str) -> bool {
    stem == "token"
        || stem.starts_with("token_")
        || stem.ends_with("-token")
        || stem.ends_with("_token")
        || matches!(stem, "credentials" | "api_key" | "apikey")
}

/// Simple glob matching supporting `*` and `?` wildcards.
pub fn glob_match(pattern: &str, text: &str) -> bool {
    let pc: Vec<char> = pattern.chars().collect();
    let tc: Vec<char> = text.chars().collect();
    glob_inner(&pc, &tc)
}

fn glob_inner(pattern: &[char], text: &[char]) -> bool {
    if pattern.is_empty() {
        return text.is_empty();
    }
    if pattern[0] == '*' {
        for i in 0..=text.len() {
            if glob_inner(&pattern[1..], &text[i..]) {
                return true;
            }
        }
        return false;
    }
    if text.is_empty() {
        return false;
    }
    if pattern[0] == '?' || pattern[0] == text[0] {
        return glob_inner(&pattern[1..], &text[1..]);
    }
    false
}

#[cfg(test)]
mod tests {
    use super::has_secret_extension;
    use std::path::Path;

    #[test]
    fn classifies_secret_names_by_boundary_and_container_extension() {
        let cases = [
            ("tokens.css", false),
            ("tokens.ts", false),
            ("token_budget.rs", false),
            ("token_tracker.py", false),
            ("local_token.rs", false),
            ("api_key_client.py", false),
            ("credentials_loader.ts", false),
            ("token-guide.md", false),
            ("token", true),
            ("token.txt", true),
            ("token_prod.json", true),
            ("access-token.toml", true),
            ("refresh_token.ini", true),
            ("credentials.yaml", true),
            ("api_key.cfg", true),
            ("apikey.xml", true),
            (".token", true),
            (".token.txt", true),
            (".credentials", true),
            (".api_key", true),
            (".apikey", true),
            (".env", true),
            (".env.local", true),
            ("settings.env", true),
            ("id_rsa", true),
            ("id_ed25519.pub", true),
            ("server.pem", true),
            ("server.crt", true),
            ("truststore.jks", true),
        ];

        for (path, expected) in cases {
            assert_eq!(
                has_secret_extension(Path::new(path)),
                expected,
                "unexpected secret classification for {path}"
            );
        }
    }
}
