//! Deterministic, no-LLM system model of the Cargo workspace.
//!
//! Leaf A of epic #886 (#887). This module reads facts straight off disk —
//! the workspace root `Cargo.toml`, each member crate's `Cargo.toml`, and the
//! `gobby-core` feature table — and turns them into a serializable
//! [`SystemModel`]: the member crates, their workspace-internal dependency
//! edges, the service boundaries the feature gates pull in, the runtime modes
//! the binaries can operate in, and the per-crate enabled `gobby-core`
//! features.
//!
//! Nothing here calls an LLM, touches the network, or hits a datastore. The
//! later diagram/infra leaves (B/D) consume this model; this leaf only lands
//! the model plus its extraction and tests.
//!
//! Robustness: a missing or malformed `Cargo.toml` degrades to a *partial*
//! model — the offending crate is skipped and a note is recorded — rather than
//! panicking or erroring out the whole build. No I/O or parse path unwraps.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use crate::index::hasher;

/// Package name of the shared foundation crate whose `[features]` table
/// defines the adapter feature gates the rest of the workspace opts into.
const CORE_PACKAGE: &str = "gobby-core";

/// A workspace member crate.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Crate {
    /// Cargo package name (e.g. `gobby-code`), not the directory name.
    pub name: String,
    /// Workspace-relative path to the crate directory (e.g. `crates/gcode`).
    pub path: String,
    /// Whether the crate ships a binary target (`[[bin]]` or `src/main.rs`).
    pub is_binary: bool,
    /// Whether the crate ships a library target (`[lib]` or `src/lib.rs`).
    pub is_lib: bool,
}

/// A workspace-internal dependency edge: `from` depends on `to`, where both
/// endpoints are member package names. Edges to external crates.io
/// dependencies are never recorded.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, serde::Serialize)]
pub struct Edge {
    /// Package name of the depending crate.
    pub from: String,
    /// Package name of the depended-upon workspace member.
    pub to: String,
}

/// A runtime service / external boundary the workspace can talk to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, serde::Serialize)]
pub enum ServiceKind {
    /// PostgreSQL hub (the `postgres` adapter feature).
    Postgres,
    /// FalkorDB code/relationship graph (the `falkor` adapter feature).
    Falkor,
    /// Qdrant vector store (the `qdrant` adapter feature).
    Qdrant,
    /// OpenAI-compatible embedding / completion API (the `ai` adapter feature,
    /// direct transport).
    EmbeddingApi,
    /// Gobby daemon (the `ai` adapter routes through it; daemon URL resolution
    /// lives in `gobby_core::daemon_url`).
    Daemon,
    /// `~/.gobby/hooks/inbox` enqueue path that `ghook` always writes to.
    GhookInbox,
    /// tree-sitter grammars: the AST parsing toolchain pulled in by a member
    /// that depends on `tree-sitter` plus its `tree-sitter-*` grammar crates.
    /// Detected from Cargo dependency names, not a `gobby-core` feature gate.
    TreeSitter,
    /// Document/Office toolchain (PDF + spreadsheet extraction) pulled in by a
    /// member exposing a `documents` feature or a `pdf-extract`/`pdfium-*`
    /// dependency. A Cargo-visible toolchain, not a `gobby-core` feature gate.
    DocumentToolchain,
    /// Media toolchain (ffmpeg, a system binary reached via `PATH`). ffmpeg
    /// leaves no Cargo signal, so this boundary is detected by probing for the
    /// media-ingest source file (`crates/gwiki/src/media.rs`).
    MediaToolchain,
}

impl ServiceKind {
    pub(crate) fn kind_slug(self) -> &'static str {
        match self {
            Self::Postgres => "postgres",
            Self::Falkor => "falkor",
            Self::Qdrant => "qdrant",
            Self::EmbeddingApi => "embedding_api",
            Self::Daemon => "daemon",
            Self::GhookInbox => "ghook_inbox",
            Self::TreeSitter => "tree_sitter",
            Self::DocumentToolchain => "document_toolchain",
            Self::MediaToolchain => "media_toolchain",
        }
    }
}

/// One service boundary the workspace reaches, plus what pulls it in.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct ServiceBoundary {
    /// Stable, human-readable identifier for the boundary.
    pub name: String,
    /// Which kind of service this is.
    pub kind: ServiceKind,
    /// Crate+feature pairs responsible for pulling the boundary in, e.g.
    /// `gobby-code (feature: postgres)` or `gobby-hooks (always)`. Sorted and
    /// de-duplicated for determinism.
    pub pulled_in_by: Vec<String>,
}

/// How a binary can run with respect to the AI routing decision.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum RuntimeMode {
    /// Direct/off routing: the CLI talks to datastores and the embedding API
    /// itself, with no daemon in the loop.
    Standalone,
    /// Daemon routing: the CLI delegates AI (and scheduling) to the Gobby
    /// daemon.
    DaemonAttached,
}

/// Deterministic, serializable model of the workspace. Built with no LLM
/// calls from facts on disk.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct SystemModel {
    /// Member crates, sorted by package name.
    pub crates: Vec<Crate>,
    /// Workspace-internal dependency edges, sorted by `(from, to)`.
    pub edges: Vec<Edge>,
    /// Service boundaries the workspace reaches, sorted by `(kind, name)`.
    pub services: Vec<ServiceBoundary>,
    /// Runtime modes the workspace can operate in (always both).
    pub runtime_modes: Vec<RuntimeMode>,
    /// Enabled `gobby-core` features per crate (package name → sorted feature
    /// list). Only members with a `gobby-core` dependency appear.
    pub features_by_crate: BTreeMap<String, Vec<String>>,
    /// Non-fatal notes recorded while extracting a partial model (e.g. a
    /// member `Cargo.toml` that could not be read or parsed). Empty for a
    /// fully-resolved workspace.
    pub notes: Vec<String>,
}

impl SystemModel {
    /// Stable content digest of the whole model, used as the per-page-type
    /// invalidation key for the architecture and infrastructure pages (Leaf H,
    /// #893). All fields are deterministically ordered (sorted vecs, a
    /// `BTreeMap`), so the canonical JSON encoding is reproducible and the
    /// digest changes only on a structural workspace change — a crate, edge,
    /// service boundary, runtime mode, or feature shift — not a function-body
    /// edit. Serialization cannot realistically fail for this owned, plain-data
    /// model; an empty encoding (and thus a stable digest) is the safe fallback.
    pub(crate) fn digest(&self) -> String {
        let encoded = serde_json::to_vec(self).unwrap_or_default();
        hasher::content_hash(&encoded)
    }
}

/// Build a [`SystemModel`] from the workspace rooted at `repo_root`.
///
/// Reads `repo_root/Cargo.toml` for `[workspace].members`, then each member's
/// `Cargo.toml`. Any unreadable/unparseable manifest is skipped with a note;
/// the function always returns a model and never panics.
pub fn build_system_model(repo_root: &Path) -> SystemModel {
    let mut notes = Vec::new();

    let members = workspace_members(repo_root, &mut notes);

    // First pass: resolve every member's package name and target shape so we
    // can recognise workspace-internal dependency edges by package name.
    let mut crates: Vec<Crate> = Vec::new();
    // (package name, parsed manifest), kept for the dependency pass.
    let mut manifests: Vec<(String, toml::Value)> = Vec::new();

    for member in &members {
        let manifest_path = repo_root.join(member).join("Cargo.toml");
        let raw = match std::fs::read_to_string(&manifest_path) {
            Ok(raw) => raw,
            Err(err) => {
                notes.push(format!(
                    "skipped member `{member}`: cannot read {}: {err}",
                    manifest_path.display()
                ));
                continue;
            }
        };
        let manifest: toml::Value = match toml::from_str::<toml::Value>(&raw) {
            Ok(value) => value,
            Err(err) => {
                notes.push(format!(
                    "skipped member `{member}`: malformed {}: {err}",
                    manifest_path.display()
                ));
                continue;
            }
        };

        let Some(name) = package_name(&manifest) else {
            notes.push(format!(
                "skipped member `{member}`: manifest has no [package].name"
            ));
            continue;
        };

        let crate_dir = repo_root.join(member);
        let is_binary =
            has_table_array(&manifest, "bin") || crate_dir.join("src/main.rs").is_file();
        let is_lib = manifest.get("lib").is_some() || crate_dir.join("src/lib.rs").is_file();

        crates.push(Crate {
            name: name.clone(),
            path: member.clone(),
            is_binary,
            is_lib,
        });
        manifests.push((name, manifest));
    }

    let member_names: BTreeSet<String> = crates.iter().map(|c| c.name.clone()).collect();

    // Second pass: dependency edges (workspace-internal only), per-crate
    // gobby-core feature sets, and the raw dependency-name / feature-key sets
    // the toolchain boundaries (tree-sitter / document / media) are detected
    // from.
    let mut edges: Vec<Edge> = Vec::new();
    let mut features_by_crate: BTreeMap<String, Vec<String>> = BTreeMap::new();
    // package name -> features enabled on its gobby-core dependency.
    let mut core_features: BTreeMap<String, Vec<String>> = BTreeMap::new();
    // package name -> every dependency name it declares (any dependency table).
    let mut dep_names_by_crate: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    // package name -> the keys of its own `[features]` table.
    let mut feature_keys_by_crate: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();

    for (name, manifest) in &manifests {
        for (dep_name, dep_value) in dependency_entries(manifest) {
            if member_names.contains(&dep_name) && dep_name != *name {
                edges.push(Edge {
                    from: name.clone(),
                    to: dep_name.clone(),
                });
            }
            if dep_name == CORE_PACKAGE {
                let feats = dependency_features(&dep_value);
                features_by_crate.insert(name.clone(), feats.clone());
                core_features.insert(name.clone(), feats);
            }
            dep_names_by_crate
                .entry(name.clone())
                .or_default()
                .insert(dep_name);
        }
        let feature_keys = feature_table_keys(manifest);
        if !feature_keys.is_empty() {
            feature_keys_by_crate.insert(name.clone(), feature_keys);
        }
    }

    edges.sort();
    edges.dedup();
    crates.sort_by(|a, b| a.name.cmp(&b.name));

    let services = service_boundaries(
        &core_features,
        &dep_names_by_crate,
        &feature_keys_by_crate,
        &crates,
        repo_root,
    );

    SystemModel {
        crates,
        edges,
        services,
        runtime_modes: vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached],
        features_by_crate,
        notes,
    }
}

/// Read `[workspace].members` from the root manifest. On any failure, records a
/// note and returns an empty member list (a fully-partial model).
fn workspace_members(repo_root: &Path, notes: &mut Vec<String>) -> Vec<String> {
    let root_manifest = repo_root.join("Cargo.toml");
    let raw = match std::fs::read_to_string(&root_manifest) {
        Ok(raw) => raw,
        Err(err) => {
            notes.push(format!(
                "cannot read workspace manifest {}: {err}",
                root_manifest.display()
            ));
            return Vec::new();
        }
    };
    let value: toml::Value = match toml::from_str::<toml::Value>(&raw) {
        Ok(value) => value,
        Err(err) => {
            notes.push(format!(
                "malformed workspace manifest {}: {err}",
                root_manifest.display()
            ));
            return Vec::new();
        }
    };
    let members = value
        .get("workspace")
        .and_then(|w| w.get("members"))
        .and_then(|m| m.as_array());
    let Some(members) = members else {
        notes.push("workspace manifest has no [workspace].members array".to_string());
        return Vec::new();
    };
    members
        .iter()
        .filter_map(|m| m.as_str().map(str::to_string))
        .collect()
}

/// Extract `[package].name` from a parsed manifest.
fn package_name(manifest: &toml::Value) -> Option<String> {
    manifest
        .get("package")
        .and_then(|p| p.get("name"))
        .and_then(|n| n.as_str())
        .map(str::to_string)
}

/// Whether the manifest declares a non-empty `[[<key>]]` table-array (e.g.
/// `[[bin]]`).
fn has_table_array(manifest: &toml::Value, key: &str) -> bool {
    manifest
        .get(key)
        .and_then(|v| v.as_array())
        .is_some_and(|arr| !arr.is_empty())
}

/// All dependency entries across `[dependencies]`, `[dev-dependencies]`, and
/// `[build-dependencies]`. Returns `(dep_name, dep_value)` pairs. Target-scoped
/// dependency tables are ignored — workspace-internal edges live in the plain
/// tables for this workspace.
fn dependency_entries(manifest: &toml::Value) -> Vec<(String, toml::Value)> {
    const TABLES: [&str; 3] = ["dependencies", "dev-dependencies", "build-dependencies"];
    let mut out = Vec::new();
    for table in TABLES {
        if let Some(deps) = manifest.get(table).and_then(|t| t.as_table()) {
            for (name, value) in deps {
                out.push((name.clone(), value.clone()));
            }
        }
    }
    out
}

/// Features enabled on a dependency entry. A bare `dep = "1"` string has no
/// features; a `dep = { features = [...] }` table contributes its `features`
/// array. Returns a sorted, de-duplicated list.
fn dependency_features(dep_value: &toml::Value) -> Vec<String> {
    let mut feats: Vec<String> = dep_value
        .as_table()
        .and_then(|t| t.get("features"))
        .and_then(|f| f.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    feats.sort();
    feats.dedup();
    feats
}

/// Keys of a manifest's own `[features]` table (e.g. `documents`, `default`).
/// Returns a sorted, de-duplicated set; an absent table yields an empty set.
fn feature_table_keys(manifest: &toml::Value) -> BTreeSet<String> {
    manifest
        .get("features")
        .and_then(|f| f.as_table())
        .map(|table| table.keys().cloned().collect())
        .unwrap_or_default()
}

/// Map a `gobby-core` adapter feature to the service boundaries it pulls in.
/// Mirrors `crates/gcore/Cargo.toml` `[features]`: `ai` enables the AI
/// transport, which can reach both the embedding API directly and the daemon.
fn feature_services(feature: &str) -> &'static [ServiceKind] {
    match feature {
        "postgres" => &[ServiceKind::Postgres],
        "falkor" => &[ServiceKind::Falkor],
        "qdrant" => &[ServiceKind::Qdrant],
        "ai" => &[ServiceKind::EmbeddingApi, ServiceKind::Daemon],
        _ => &[],
    }
}

/// Stable display name for a service kind.
fn service_name(kind: ServiceKind) -> &'static str {
    match kind {
        ServiceKind::Postgres => "PostgreSQL hub",
        ServiceKind::Falkor => "FalkorDB graph",
        ServiceKind::Qdrant => "Qdrant vectors",
        ServiceKind::EmbeddingApi => "Embedding API",
        ServiceKind::Daemon => "Gobby daemon",
        ServiceKind::GhookInbox => "ghook inbox",
        ServiceKind::TreeSitter => "tree-sitter grammars",
        ServiceKind::DocumentToolchain => "Document toolchain (PDF/Office)",
        ServiceKind::MediaToolchain => "Media toolchain (ffmpeg)",
    }
}

/// Derive the service boundaries from the per-crate `gobby-core` feature sets,
/// plus the two always-present boundaries: `ghook` always enqueues to its
/// inbox, and the daemon URL resolver lives in always-compiled
/// `gobby_core::daemon_url`.
fn service_boundaries(
    core_features: &BTreeMap<String, Vec<String>>,
    dep_names_by_crate: &BTreeMap<String, BTreeSet<String>>,
    feature_keys_by_crate: &BTreeMap<String, BTreeSet<String>>,
    crates: &[Crate],
    repo_root: &Path,
) -> Vec<ServiceBoundary> {
    // kind -> set of "crate (feature: x)" provenance strings.
    let mut by_kind: BTreeMap<ServiceKind, BTreeSet<String>> = BTreeMap::new();

    for (crate_name, feats) in core_features {
        for feat in feats {
            for kind in feature_services(feat) {
                by_kind
                    .entry(*kind)
                    .or_default()
                    .insert(format!("{crate_name} (feature: {feat})"));
            }
        }
    }

    // ghook always enqueues to ~/.gobby/hooks/inbox regardless of features.
    // Attribute the boundary to the hook-dispatcher member (bin-only crate at
    // crates/ghook) when present, else to the workspace generically.
    let ghook_owner = crates
        .iter()
        .find(|c| c.is_binary && !c.is_lib && c.path == "crates/ghook")
        .map(|c| c.name.clone());
    let inbox_provenance = match ghook_owner {
        Some(name) => format!("{name} (always)"),
        None => "workspace (always)".to_string(),
    };
    by_kind
        .entry(ServiceKind::GhookInbox)
        .or_default()
        .insert(inbox_provenance);

    // The daemon URL resolver is always-compiled in gobby_core::daemon_url, so
    // the daemon boundary exists for the workspace even absent the `ai`
    // feature. Confirm gcore is present before asserting this.
    if repo_root.join("crates/gcore/Cargo.toml").is_file() {
        by_kind
            .entry(ServiceKind::Daemon)
            .or_default()
            .insert("workspace (gobby_core::daemon_url, always)".to_string());
    }

    // Toolchain boundaries (tree-sitter / document / media) detected from real
    // on-disk facts rather than gobby-core feature gates. Each contributes a
    // provenance string keyed by kind, merged into the same map so the final
    // sort/dedup keeps everything deterministic.
    for (kind, provenance) in
        toolchain_boundaries(dep_names_by_crate, feature_keys_by_crate, crates, repo_root)
    {
        by_kind.entry(kind).or_default().insert(provenance);
    }

    let mut services: Vec<ServiceBoundary> = by_kind
        .into_iter()
        .map(|(kind, provenance)| ServiceBoundary {
            name: service_name(kind).to_string(),
            kind,
            pulled_in_by: provenance.into_iter().collect(),
        })
        .collect();
    services.sort_by(|a, b| (a.kind, &a.name).cmp(&(b.kind, &b.name)));
    services
}

/// Detect the toolchain service boundaries the workspace reaches from real
/// on-disk Cargo facts plus a file probe (ffmpeg leaves no Cargo signal):
///
/// - **tree-sitter**: any member that depends on a crate named exactly
///   `tree-sitter`; provenance counts that crate's `tree-sitter-*` grammar
///   dependencies.
/// - **document toolchain**: any member exposing a `documents` feature or a
///   `pdf-extract` / `pdfium-render` / `pdfium-auto` dependency.
/// - **media toolchain**: the `crates/gwiki` member when its `src/media.rs`
///   exists on disk (the same kind of probe the always-on daemon boundary
///   uses for `crates/gcore/Cargo.toml`).
///
/// Returns `(kind, provenance)` pairs; an absent toolchain yields no pair, so
/// the boundary is simply omitted from the model.
fn toolchain_boundaries(
    dep_names_by_crate: &BTreeMap<String, BTreeSet<String>>,
    feature_keys_by_crate: &BTreeMap<String, BTreeSet<String>>,
    crates: &[Crate],
    repo_root: &Path,
) -> Vec<(ServiceKind, String)> {
    const PDF_DEPS: [&str; 3] = ["pdf-extract", "pdfium-render", "pdfium-auto"];
    let mut out: Vec<(ServiceKind, String)> = Vec::new();

    for (crate_name, deps) in dep_names_by_crate {
        // tree-sitter: a `tree-sitter` dependency plus N `tree-sitter-*`
        // grammar crates.
        if deps.contains("tree-sitter") {
            let grammar_count = deps
                .iter()
                .filter(|dep| dep.starts_with("tree-sitter-"))
                .count();
            out.push((
                ServiceKind::TreeSitter,
                format!("{crate_name} (deps: tree-sitter + {grammar_count} grammars)"),
            ));
        }

        // Document toolchain: a `documents` feature key OR a PDF/Office dep.
        let has_documents_feature = feature_keys_by_crate
            .get(crate_name)
            .is_some_and(|keys| keys.contains("documents"));
        let has_pdf_dep = PDF_DEPS.iter().any(|dep| deps.contains(*dep));
        if has_documents_feature || has_pdf_dep {
            out.push((
                ServiceKind::DocumentToolchain,
                format!("{crate_name} (feature: documents)"),
            ));
        }
    }

    // Media toolchain: ffmpeg via PATH, detected by probing the gwiki crate's
    // media-ingest source file. Derive the gwiki package name from the model
    // by its workspace-relative crate path.
    if let Some(gwiki) = crates.iter().find(|c| c.path == "crates/gwiki")
        && repo_root.join(&gwiki.path).join("src/media.rs").is_file()
    {
        out.push((
            ServiceKind::MediaToolchain,
            format!("{} (src/media.rs, ffmpeg via PATH)", gwiki.name),
        ));
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    /// Write a minimal fixture workspace under a fresh temp dir and return its
    /// root path (kept alive by the returned `TempDir`).
    fn fixture_workspace(members: &[(&str, &str)]) -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().expect("create temp dir");
        let root = dir.path().to_path_buf();
        let member_list = members
            .iter()
            .map(|(path, _)| format!("\"{path}\""))
            .collect::<Vec<_>>()
            .join(", ");
        fs::write(
            root.join("Cargo.toml"),
            format!("[workspace]\nmembers = [{member_list}]\nresolver = \"3\"\n"),
        )
        .expect("write root manifest");
        for (path, manifest) in members {
            let crate_dir = root.join(path);
            fs::create_dir_all(crate_dir.join("src")).expect("create crate dir");
            fs::write(crate_dir.join("Cargo.toml"), manifest).expect("write member manifest");
        }
        (dir, root)
    }

    fn crate_named<'a>(model: &'a SystemModel, name: &str) -> &'a Crate {
        model
            .crates
            .iter()
            .find(|c| c.name == name)
            .unwrap_or_else(|| panic!("crate `{name}` missing from model"))
    }

    #[test]
    fn extracts_crates_internal_edges_and_target_shape() {
        // A lib-only foundation crate and a bin crate that depends on it. The
        // dir name (`app`) differs from the package name (`my-app`) to exercise
        // package-name resolution for edges.
        let lib_manifest = "[package]\nname = \"my-core\"\nversion = \"0.1.0\"\n\n[lib]\nname = \"my_core\"\npath = \"src/lib.rs\"\n";
        let bin_manifest = "[package]\nname = \"my-app\"\nversion = \"0.1.0\"\n\n[[bin]]\nname = \"app\"\npath = \"src/main.rs\"\n\n[dependencies]\nmy-core = { path = \"../core\" }\nserde = \"1\"\n";
        let (_dir, root) =
            fixture_workspace(&[("crates/core", lib_manifest), ("crates/app", bin_manifest)]);

        let model = build_system_model(&root);

        assert!(
            model.notes.is_empty(),
            "unexpected notes: {:?}",
            model.notes
        );
        assert_eq!(model.crates.len(), 2);

        // crates are sorted by package name: my-app, my-core.
        let app = crate_named(&model, "my-app");
        assert!(app.is_binary);
        assert!(!app.is_lib);
        assert_eq!(app.path, "crates/app");

        let core = crate_named(&model, "my-core");
        assert!(!core.is_binary);
        assert!(core.is_lib);

        // Only the workspace-internal edge is recorded; serde (crates.io) is
        // not an edge.
        assert_eq!(
            model.edges,
            vec![Edge {
                from: "my-app".to_string(),
                to: "my-core".to_string(),
            }]
        );

        // Both runtime modes are always present.
        assert!(model.runtime_modes.contains(&RuntimeMode::Standalone));
        assert!(model.runtime_modes.contains(&RuntimeMode::DaemonAttached));
    }

    #[test]
    fn maps_core_features_to_service_boundaries() {
        // The foundation crate is named gobby-core so feature resolution
        // matches the real adapter naming; a member enables postgres+qdrant.
        let core_manifest = "[package]\nname = \"gobby-core\"\nversion = \"0.5.0\"\n\n[lib]\nname = \"gobby_core\"\npath = \"src/lib.rs\"\n\n[features]\npostgres = []\nqdrant = []\nfalkor = []\nai = []\n";
        let consumer_manifest = "[package]\nname = \"gobby-code\"\nversion = \"1.0.0\"\n\n[[bin]]\nname = \"gcode\"\npath = \"src/main.rs\"\n\n[dependencies]\ngobby-core = { path = \"../gcore\", features = [\"postgres\", \"qdrant\"] }\n";
        let (_dir, root) = fixture_workspace(&[
            ("crates/gcore", core_manifest),
            ("crates/gcode", consumer_manifest),
        ]);

        let model = build_system_model(&root);
        assert!(
            model.notes.is_empty(),
            "unexpected notes: {:?}",
            model.notes
        );

        // features_by_crate records the enabled gobby-core features, sorted.
        assert_eq!(
            model.features_by_crate.get("gobby-code"),
            Some(&vec!["postgres".to_string(), "qdrant".to_string()])
        );

        // Postgres boundary, pulled in by gobby-code (feature: postgres).
        let pg = model
            .services
            .iter()
            .find(|s| s.kind == ServiceKind::Postgres)
            .expect("Postgres boundary present");
        assert_eq!(
            pg.pulled_in_by,
            vec!["gobby-code (feature: postgres)".to_string()]
        );

        // Qdrant boundary, pulled in by gobby-code (feature: qdrant).
        let qd = model
            .services
            .iter()
            .find(|s| s.kind == ServiceKind::Qdrant)
            .expect("Qdrant boundary present");
        assert_eq!(
            qd.pulled_in_by,
            vec!["gobby-code (feature: qdrant)".to_string()]
        );

        // No `ai` feature was enabled, so there is no EmbeddingApi boundary.
        assert!(
            !model
                .services
                .iter()
                .any(|s| s.kind == ServiceKind::EmbeddingApi),
            "EmbeddingApi must not appear without the ai feature"
        );

        // The daemon boundary is always present (gobby_core::daemon_url) and
        // the ghook inbox boundary is always present.
        assert!(model.services.iter().any(|s| s.kind == ServiceKind::Daemon));
        assert!(
            model
                .services
                .iter()
                .any(|s| s.kind == ServiceKind::GhookInbox)
        );
    }

    #[test]
    fn ai_feature_pulls_in_embedding_api_and_daemon() {
        let core_manifest = "[package]\nname = \"gobby-core\"\nversion = \"0.5.0\"\n\n[lib]\nname = \"gobby_core\"\npath = \"src/lib.rs\"\n\n[features]\nai = []\n";
        let consumer_manifest = "[package]\nname = \"gobby-wiki\"\nversion = \"0.5.0\"\n\n[[bin]]\nname = \"gwiki\"\npath = \"src/main.rs\"\n\n[dependencies]\ngobby-core = { path = \"../gcore\", features = [\"ai\"] }\n";
        let (_dir, root) = fixture_workspace(&[
            ("crates/gcore", core_manifest),
            ("crates/gwiki", consumer_manifest),
        ]);

        let model = build_system_model(&root);

        let embed = model
            .services
            .iter()
            .find(|s| s.kind == ServiceKind::EmbeddingApi)
            .expect("EmbeddingApi boundary present");
        assert_eq!(
            embed.pulled_in_by,
            vec!["gobby-wiki (feature: ai)".to_string()]
        );

        // Daemon is pulled in both by the ai feature AND the always-on
        // daemon_url resolver; both provenance strings appear (sorted).
        let daemon = model
            .services
            .iter()
            .find(|s| s.kind == ServiceKind::Daemon)
            .expect("Daemon boundary present");
        assert!(
            daemon
                .pulled_in_by
                .contains(&"gobby-wiki (feature: ai)".to_string())
        );
        assert!(daemon.pulled_in_by.iter().any(|p| p.contains("daemon_url")));
    }

    #[test]
    fn degrades_to_partial_model_on_missing_and_malformed_manifests() {
        let good_manifest = "[package]\nname = \"good-crate\"\nversion = \"0.1.0\"\n\n[lib]\npath = \"src/lib.rs\"\n";
        let (_dir, root) = fixture_workspace(&[("crates/good", good_manifest)]);

        // Rewrite the root manifest to list two extra members that do not have
        // valid manifests on disk: one missing entirely, one malformed.
        fs::write(
            root.join("Cargo.toml"),
            "[workspace]\nmembers = [\"crates/good\", \"crates/missing\", \"crates/broken\"]\n",
        )
        .expect("rewrite root manifest");
        // Malformed member manifest (invalid TOML).
        fs::create_dir_all(root.join("crates/broken")).expect("create broken dir");
        fs::write(
            root.join("crates/broken/Cargo.toml"),
            "this is not = valid toml [[[",
        )
        .expect("write broken manifest");
        // crates/missing has no Cargo.toml at all.

        let model = build_system_model(&root);

        // Only the good crate survives; no panic occurred.
        assert_eq!(model.crates.len(), 1);
        assert_eq!(model.crates[0].name, "good-crate");

        // Two notes: the missing read and the malformed parse.
        assert_eq!(model.notes.len(), 2, "notes: {:?}", model.notes);
        assert!(model.notes.iter().any(|n| n.contains("crates/missing")));
        assert!(model.notes.iter().any(|n| n.contains("crates/broken")));

        // Runtime modes still both present even in a partial model.
        assert_eq!(
            model.runtime_modes,
            vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached]
        );
    }

    #[test]
    fn missing_workspace_manifest_yields_empty_partial_model() {
        let dir = tempfile::tempdir().expect("temp dir");
        let model = build_system_model(dir.path());
        assert!(model.crates.is_empty());
        assert!(model.edges.is_empty());
        assert_eq!(model.notes.len(), 1);
        assert!(model.notes[0].contains("cannot read workspace manifest"));
        // Runtime modes are unconditional.
        assert_eq!(
            model.runtime_modes,
            vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached]
        );
    }

    #[test]
    fn tree_sitter_dep_yields_tree_sitter_boundary_with_grammar_count() {
        // A member that depends on `tree-sitter` plus two grammar crates pulls
        // in the TreeSitter boundary; the provenance counts the grammars.
        let manifest = "[package]\nname = \"parser-crate\"\nversion = \"0.1.0\"\n\n[lib]\npath = \"src/lib.rs\"\n\n[dependencies]\ntree-sitter = \"0.25\"\ntree-sitter-rust = \"0.24\"\ntree-sitter-python = \"0.25\"\nserde = \"1\"\n";
        let (_dir, root) = fixture_workspace(&[("crates/parser", manifest)]);

        let model = build_system_model(&root);
        assert!(
            model.notes.is_empty(),
            "unexpected notes: {:?}",
            model.notes
        );

        let ts = model
            .services
            .iter()
            .find(|s| s.kind == ServiceKind::TreeSitter)
            .expect("TreeSitter boundary present");
        assert_eq!(ts.name, "tree-sitter grammars");
        assert_eq!(
            ts.pulled_in_by,
            vec!["parser-crate (deps: tree-sitter + 2 grammars)".to_string()]
        );
    }

    #[test]
    fn documents_feature_yields_document_toolchain_boundary() {
        // A member exposing a `documents` feature pulls in the DocumentToolchain
        // boundary even without any PDF dependency named explicitly.
        let manifest = "[package]\nname = \"vault-crate\"\nversion = \"0.1.0\"\n\n[lib]\npath = \"src/lib.rs\"\n\n[features]\ndefault = [\"documents\"]\ndocuments = [\"dep:pdf-extract\"]\n\n[dependencies]\npdf-extract = { version = \"0.10\", optional = true }\n";
        let (_dir, root) = fixture_workspace(&[("crates/vault", manifest)]);

        let model = build_system_model(&root);
        assert!(
            model.notes.is_empty(),
            "unexpected notes: {:?}",
            model.notes
        );

        let docs = model
            .services
            .iter()
            .find(|s| s.kind == ServiceKind::DocumentToolchain)
            .expect("DocumentToolchain boundary present");
        assert_eq!(docs.name, "Document toolchain (PDF/Office)");
        assert_eq!(
            docs.pulled_in_by,
            vec!["vault-crate (feature: documents)".to_string()]
        );
    }

    #[test]
    fn workspace_without_toolchains_omits_those_boundaries() {
        // A plain lib crate with no tree-sitter dep, no documents feature, and
        // no crates/gwiki member yields none of the toolchain boundaries.
        let manifest = "[package]\nname = \"plain-crate\"\nversion = \"0.1.0\"\n\n[lib]\npath = \"src/lib.rs\"\n\n[dependencies]\nserde = \"1\"\n";
        let (_dir, root) = fixture_workspace(&[("crates/plain", manifest)]);

        let model = build_system_model(&root);
        assert!(
            model.notes.is_empty(),
            "unexpected notes: {:?}",
            model.notes
        );

        assert!(
            !model
                .services
                .iter()
                .any(|s| s.kind == ServiceKind::TreeSitter),
            "TreeSitter must be omitted when no tree-sitter dep exists"
        );
        assert!(
            !model
                .services
                .iter()
                .any(|s| s.kind == ServiceKind::DocumentToolchain),
            "DocumentToolchain must be omitted with no documents feature / pdf dep"
        );
        assert!(
            !model
                .services
                .iter()
                .any(|s| s.kind == ServiceKind::MediaToolchain),
            "MediaToolchain must be omitted without a crates/gwiki member"
        );
    }
}
