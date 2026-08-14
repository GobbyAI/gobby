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
        r#"postgres = ["dep:postgres", "dep:postgres-openssl", "dep:base64", "dep:scrypt", "dep:sha2", "dep:time"]"#,
        r#"falkor = ["dep:redis"]"#,
        r#"qdrant = ["dep:reqwest", "dep:urlencoding"]"#,
        r#"indexing = ["dep:ignore", "dep:sha2"]"#,
        "search = []",
        "graph-analytics = []",
        r#"ai = ["dep:reqwest", "dep:base64", "dep:bytes", "dep:httpdate", "dep:rand", "reqwest/multipart"]"#,
        r#"full = ["postgres", "falkor", "qdrant", "indexing", "search", "graph-analytics", "ai"]"#,
        r#"serde = { version = "1", features = ["derive"] }"#,
        r#"thiserror = "2""#,
        r#"postgres = { version = "0.19", optional = true, features = ["with-uuid-1"] }"#,
        r#"postgres-openssl = { version = "0.5", optional = true }"#,
        r#"openssl = { version = "0.10", features = ["vendored"] }"#,
        r#"redis = { version = "0.32", optional = true, default-features = false }"#,
        r#"reqwest = { version = "0.12", default-features = false, features = ["blocking", "json", "rustls-tls"], optional = true }"#,
        r#"base64 = { version = "0.22", optional = true }"#,
        r#"fernet = "0.2.2""#,
        r#"scrypt = { version = "0.11", optional = true }"#,
        r#"bytes = { version = "1", optional = true }"#,
        r#"httpdate = { version = "1", optional = true }"#,
        r#"rand = { version = "0.8", optional = true }"#,
        r#"ureq = { version = "2", features = ["json"] }"#,
        r#"ignore = { version = "0.4", optional = true }"#,
        r#"sha2 = { version = "0.11", optional = true }"#,
        r#"time = { version = "0.3", features = ["parsing"], optional = true }"#,
        r#"urlencoding = { version = "2", optional = true }"#,
    ] {
        assert!(
            manifest.contains(expected),
            "Cargo.toml is missing expected public-boundary snippet: {expected}"
        );
    }
    assert!(
        !manifest.contains("postgres-types"),
        "Cargo.toml must not declare the retired postgres-types dependency"
    );
}

#[test]
fn lib_rs_exposes_lightweight_and_feature_gated_modules() {
    let lib_rs = crate_file("src/lib.rs");

    for expected in [
        "pub mod bootstrap;",
        "pub mod daemon_url;",
        "pub mod project;",
        "pub mod ai_context;",
        "pub mod ai_types;",
        "pub mod codewiki_contract;",
        "pub mod config;",
        "pub mod degradation;",
        r#"#[cfg(test)]"#,
        "pub(crate) mod test_http;",
        r#"#[cfg(feature = "ai")]"#,
        "pub mod ai;",
        r#"#[cfg(feature = "postgres")]"#,
        "pub mod postgres;",
        "pub mod schema;",
        r#"#[cfg(feature = "falkor")]"#,
        "pub mod falkor;",
        r#"#[cfg(feature = "qdrant")]"#,
        "pub mod qdrant;",
        r#"#[cfg(feature = "indexing")]"#,
        "pub mod indexing;",
        r#"#[cfg(feature = "search")]"#,
        "pub mod search;",
        r#"#[cfg(feature = "graph-analytics")]"#,
        "pub mod graph_analytics;",
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
        "shared Rust foundation crate",
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
