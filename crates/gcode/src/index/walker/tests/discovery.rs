use super::*;

#[test]
fn discovers_ast_and_content_only_text_files() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "README.md", b"# Title\n");
    write_file(root, "skills/gcode/SKILL.md", b"# gcode\n");
    write_file(root, "src/lib.rs", b"fn main() {}\n");
    write_file(root, "src/module.mjs", b"export const value = 1;\n");
    write_file(root, "src/token_budget.rs", b"pub const BUDGET: u8 = 1;\n");
    write_file(root, "src/token_tracker.py", b"TOKENS = 1\n");
    write_file(root, "src/local_token.rs", b"pub const LOCAL: u8 = 1;\n");
    write_file(root, "src/tokens.ts", b"export const tokens = {};\n");
    write_file(root, "src/api_key_client.py", b"class ApiKeyClient: pass\n");
    write_file(root, "styles/tokens.css", b":root { --light: white; }\n");
    write_file(root, "docs/reference.markdown", b"# Reference\n");
    write_file(root, "docs/guide.rst", b"Guide\n=====\n");
    write_file(root, "notes.txt", b"plain notes\n");
    write_file(root, "config/app.properties", b"mode=dev\n");
    write_file(root, "config/app.toml", b"mode = 'dev'\n");
    write_file(root, "scripts/setup.sh", b"#!/usr/bin/env bash\n");
    write_file(root, "Dockerfile", b"FROM rust:latest\n");
    write_file(root, "image.bin", b"PNG\0binary");
    write_file(root, "api_key.txt", b"secret-ish\n");
    write_file(root, "secrets/token.txt", b"secret-ish\n");
    write_file(root, "config/credentials.json", b"{}\n");
    write_file(root, "keys/id_rsa", b"secret-ish\n");
    write_file(root, "certs/server.pem", b"secret-ish\n");
    write_file(root, "target/generated.txt", b"generated\n");

    let excludes = vec!["target".to_string()];
    let (ast, content_only) = discover_files(root, &excludes);

    // Secret containers, private keys, certificates, binary files, and explicit
    // excludes are omitted before AST/content-only routing.
    assert_eq!(
        rels(root, ast),
        vec![
            "scripts/setup.sh",
            "src/api_key_client.py",
            "src/lib.rs",
            "src/local_token.rs",
            "src/module.mjs",
            "src/token_budget.rs",
            "src/token_tracker.py",
            "src/tokens.ts"
        ]
    );
    assert_eq!(
        rels(root, content_only),
        vec![
            "Dockerfile",
            "README.md",
            "config/app.properties",
            "config/app.toml",
            "docs/guide.rst",
            "docs/reference.markdown",
            "notes.txt",
            "skills/gcode/SKILL.md",
            "styles/tokens.css"
        ]
    );
}

#[test]
fn discover_files_respects_gitignore_by_default_and_option() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    std::fs::create_dir(root.join(".git")).expect("git dir");
    write_file(root, ".gitignore", b"ignored.rs\n");
    write_file(root, "ignored.rs", b"fn ignored() {}\n");
    write_file(root, "src/lib.rs", b"fn visible() {}\n");

    let (default_ast, _) = discover_files(root, &[] as &[&str]);
    let default_rels = rels(root, default_ast);
    assert!(default_rels.contains(&"src/lib.rs".to_string()));
    assert!(!default_rels.contains(&"ignored.rs".to_string()));

    let (disabled_ast, _) = discover_files_with_options(
        root,
        &[] as &[&str],
        DiscoveryOptions {
            respect_gitignore: false,
        },
    );
    assert!(rels(root, disabled_ast).contains(&"ignored.rs".to_string()));
}
