use std::fs;
use std::path::PathBuf;

fn crate_file(path: &str) -> String {
    fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(path))
        .unwrap_or_else(|err| panic!("failed to read {path}: {err}"))
}

fn repo_file(path: &str) -> String {
    fs::read_to_string(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join(path),
    )
    .unwrap_or_else(|err| panic!("failed to read {path}: {err}"))
}

#[test]
fn cargo_features_define_public_boundary() {
    let manifest = crate_file("Cargo.toml");

    for expected in [
        "default = []",
        r#"postgres = ["dep:postgres", "dep:postgres-types", "dep:postgres-native-tls", "dep:native-tls"]"#,
        r#"falkor = ["dep:falkordb", "dep:urlencoding"]"#,
        r#"qdrant = ["dep:reqwest"]"#,
        r#"indexing = ["dep:ignore", "dep:sha2"]"#,
        "search = []",
        r#"full = ["postgres", "falkor", "qdrant", "indexing", "search"]"#,
        r#"serde = { version = "1", features = ["derive"] }"#,
        r#"thiserror = "2""#,
        r#"postgres = { version = "0.19", optional = true }"#,
        r#"postgres-types = { version = "0.2", optional = true }"#,
        r#"postgres-native-tls = { version = "0.5", optional = true }"#,
        r#"native-tls = { version = "0.2", optional = true }"#,
        r#"falkordb = { version = "0.2", optional = true }"#,
        r#"reqwest = { version = "0.12", default-features = false, features = ["blocking", "json", "rustls-tls"], optional = true }"#,
        r#"ignore = { version = "0.4", optional = true }"#,
        r#"sha2 = { version = "0.10", optional = true }"#,
        r#"urlencoding = { version = "2", optional = true }"#,
    ] {
        assert!(
            manifest.contains(expected),
            "Cargo.toml is missing expected public-boundary snippet: {expected}"
        );
    }
}

#[test]
fn lib_rs_exposes_lightweight_and_feature_gated_modules() {
    let lib_rs = crate_file("src/lib.rs");

    for expected in [
        "pub mod bootstrap;",
        "pub mod daemon_url;",
        "pub mod project;",
        "pub mod config;",
        "pub mod context;",
        "pub mod degradation;",
        "pub mod setup;",
        r#"#[cfg(feature = "postgres")]"#,
        "pub mod postgres;",
        r#"#[cfg(feature = "falkor")]"#,
        "pub mod falkor;",
        r#"#[cfg(feature = "qdrant")]"#,
        "pub mod qdrant;",
        r#"#[cfg(feature = "indexing")]"#,
        "pub mod indexing;",
        r#"#[cfg(feature = "search")]"#,
        "pub mod search;",
    ] {
        assert!(
            lib_rs.contains(expected),
            "lib.rs is missing expected public-boundary snippet: {expected}"
        );
    }
}

#[test]
fn development_guide_documents_foundation_boundary() {
    let guide = repo_file("docs/guides/gcore-development-guide.md");

    for expected in [
        "shared Rust migration substrate",
        "Feature Gates",
        "`postgres`",
        "`falkor`",
        "`qdrant`",
        "`indexing`",
        "`search`",
        "`full`",
        "Feature-gated modules",
        "Adding a New Helper",
    ] {
        assert!(
            guide.contains(expected),
            "development guide is missing expected public-boundary text: {expected}"
        );
    }
}
