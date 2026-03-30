//! Security checks for code indexing.
//! Ports logic from src/gobby/code_index/security.py.

use std::path::Path;

const SECRET_EXTENSIONS: &[&str] = &[
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".secret",
];

const SECRET_PREFIXES: &[&str] = &["credentials", ".env", "id_rsa", "id_ed25519", "token"];

const SECRET_SUBSTRINGS: &[&str] = &["api_key", "apikey", "_secret.", "_token."];

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

/// Check if file appears to be binary (has null bytes in first 8KB).
pub fn is_binary(path: &Path) -> bool {
    let data = match std::fs::read(path) {
        Ok(d) => d,
        Err(_) => return true,
    };
    let check_len = data.len().min(8192);
    data[..check_len].contains(&0)
}

/// Check if any path component matches an exclusion pattern.
pub fn should_exclude(path: &Path, patterns: &[String]) -> bool {
    for pattern in patterns {
        for component in path.components() {
            let name = component.as_os_str().to_string_lossy();
            if glob_match(pattern, &name) {
                return true;
            }
        }
    }
    false
}

/// Check if file extension suggests secret content.
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
    for prefix in SECRET_PREFIXES {
        if name.starts_with(prefix) {
            return true;
        }
    }
    for substring in SECRET_SUBSTRINGS {
        if name.contains(substring) {
            return true;
        }
    }
    false
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
