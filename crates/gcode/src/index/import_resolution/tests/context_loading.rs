use super::common::*;
use std::fs;

#[test]
fn loads_rust_inline_table_dependency_names() {
    let tempdir = TempDir::new().expect("tempdir");
    manifest_dir(
        tempdir.path(),
        "",
        r#"
[package]
name = "app"

[dependencies]
serde = { version = "1.0" }
"tokio-util" = { version = "0.7", features = ["codec"] }
"#,
    );

    let crates = load_rust_external_crates(tempdir.path());

    assert!(crates.contains("serde"));
    assert!(crates.contains("tokio_util"));
}

#[test]
fn loads_rust_dependency_names_from_real_toml_tables() {
    let tempdir = TempDir::new().expect("tempdir");
    manifest_dir(
        tempdir.path(),
        "",
        r#"
[package]
name = "app"

[dependencies]
serde = "1" # keep comment parsing delegated to TOML

[dev-dependencies]
pretty-assertions = "1"

[build-dependencies]
bindgen = "0.69"

[target.'cfg(unix)'.dependencies]
nix = "0.27"

[target.x86_64-pc-windows-msvc.dev-dependencies]
windows-sys = "0.52"

[target.'cfg(target_os = "linux")'.build-dependencies]
cc = "1"
"#,
    );

    let crates = load_rust_external_crates(tempdir.path());

    for name in [
        "serde",
        "pretty_assertions",
        "bindgen",
        "nix",
        "windows_sys",
        "cc",
    ] {
        assert!(crates.contains(name), "missing {name}");
    }
}

#[test]
fn ignores_rust_non_dependency_toml_tables() {
    let tempdir = TempDir::new().expect("tempdir");
    manifest_dir(
        tempdir.path(),
        "",
        r#"
[package]
name = "app"

[workspace.dependencies]
workspace-only = "1"

[package.metadata.dependencies]
metadata-only = "1"

[features]
serde = []
"#,
    );

    let crates = load_rust_external_crates(tempdir.path());

    assert!(!crates.contains("workspace_only"));
    assert!(!crates.contains("metadata_only"));
    assert!(!crates.contains("serde"));
}

#[test]
fn normalizes_rust_package_name_from_cargo_toml() {
    let tempdir = TempDir::new().expect("tempdir");
    manifest_dir(
        tempdir.path(),
        "",
        r#"
[package]
name = "my-crate"
"#,
    );

    assert_eq!(
        load_rust_self_crate_name(tempdir.path()).as_deref(),
        Some("my_crate")
    );
}

#[test]
fn loads_rust_workspace_glob_dependency_names() {
    let tempdir = TempDir::new().expect("tempdir");
    manifest_dir(
        tempdir.path(),
        "",
        r#"
[workspace]
members = ["crates/*"]
"#,
    );
    manifest_dir(
        tempdir.path(),
        "crates/app",
        r#"
[package]
name = "app"

[dependencies]
serde-json = "1"
"#,
    );
    manifest_dir(
        tempdir.path(),
        "crates/lib",
        r#"
[package]
name = "lib"

[dependencies]
tokio-util = "0.7"
"#,
    );

    let crates = load_rust_external_crates(tempdir.path());

    assert!(crates.contains("serde_json"));
    assert!(crates.contains("tokio_util"));
}

#[test]
fn missing_dart_pubspec_loads_no_external_packages() {
    let tempdir = TempDir::new().expect("tempdir");

    let packages = load_dart_external_packages(tempdir.path());

    assert!(packages.is_empty());
}

#[test]
fn loads_js_bundled_dependency_arrays() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("package.json"),
        r#"
{
  "dependencies": {
    "react": "18"
  },
  "bundledDependencies": ["left-pad"],
  "bundleDependencies": ["legacy-pkg"]
}
"#,
    )
    .expect("package json");

    let packages = load_js_external_packages(tempdir.path());

    assert!(packages.contains("react"));
    assert!(packages.contains("left-pad"));
    assert!(packages.contains("legacy-pkg"));
}

#[test]
fn bundled_import_root_json_parses() {
    assert_eq!(
        bundled_ruby_require_roots().get("json").map(String::as_str),
        Some("JSON")
    );
    assert!(
        bundled_elixir_dependency_roots()
            .get("jason")
            .is_some_and(|roots| roots.iter().any(|root| root == "Jason"))
    );
}

#[test]
fn loads_elixir_mix_lock_first_quoted_dependency_per_line() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("mix.lock"),
        r#"%{
  "jason": {:hex, :jason, "1.4.4", "checksum", [:mix], [], "hexpm", "repo"},
  "httpoison": {:hex, :httpoison, "2.2.1", "checksum", [:mix], [], "hexpm", "repo"}
}"#,
    )
    .expect("mix.lock");

    let deps = load_elixir_dependency_names(tempdir.path());

    assert!(deps.contains("jason"));
    assert!(deps.contains("httpoison"));
    assert!(!deps.contains("1"));
    assert!(!deps.contains("hexpm"));
}

#[test]
fn loads_elixir_mix_dependencies_with_whole_file_regex() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("mix.exs"),
        r#"
defp deps do
  [
    {
      :jason,
      "~> 1.4"
    },
    {:plug_cowboy,
     "~> 2.7"}
  ]
end
"#,
    )
    .expect("mix.exs");

    let deps = load_elixir_dependency_names(tempdir.path());

    assert!(deps.contains("jason"));
    assert!(deps.contains("plug_cowboy"));
}

#[test]
fn bundled_import_root_data_loads_known_mappings() {
    assert_eq!(ruby_require_root("json"), Some("JSON"));
    assert_eq!(ruby_require_root("unknown_gem"), None);

    let roots = elixir_dependency_roots("jason").expect("jason roots");
    assert_eq!(roots.first().map(String::as_str), Some("Jason"));
    assert_eq!(roots.len(), 1);
    assert!(elixir_dependency_roots("unknown_dep").is_none());
}

#[test]
fn runtime_import_root_overrides_take_precedence() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("mix.exs"),
        r#"
defp deps do
  [
    {:jason, "~> 1.4"}
  ]
end
"#,
    )
    .expect("mix.exs");

    let context = build_import_resolution_context_with_overrides(
        tempdir.path(),
        &[],
        HashMap::from([("json".to_string(), "RuntimeJSON".to_string())]),
        HashMap::from([
            ("Jason".to_string(), "RuntimeJason".to_string()),
            ("RuntimeOnly".to_string(), "RuntimeOnly".to_string()),
        ]),
    );

    let mut extracted = ExtractedImports::default();
    parse_import_statement(
        "ruby",
        r#"require "json""#,
        "app.rb",
        &context,
        &mut extracted,
    )
    .expect("parse Ruby require");
    assert!(
        extracted
            .bindings
            .external_roots
            .contains_key("RuntimeJSON")
    );
    assert!(!extracted.bindings.external_roots.contains_key("JSON"));

    let mut bindings = ImportBindings::default();
    seed_import_bindings("elixir", &context, &mut bindings);
    assert_eq!(
        bindings
            .external_roots
            .get("Jason")
            .map(|binding| binding.module.as_str()),
        Some("RuntimeJason")
    );
    assert_eq!(
        bindings
            .external_roots
            .get("RuntimeOnly")
            .map(|binding| binding.module.as_str()),
        Some("RuntimeOnly")
    );
}
