use super::*;
use std::path::{Path, PathBuf};

/// Resolve the workspace repo root from this crate's manifest dir.
/// `CARGO_MANIFEST_DIR` points at `crates/gcode`, so two levels up is the root.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn real_model_infrastructure_cites_only_real_modules_on_disk() {
    let root = repo_root();
    let model = build_system_model(&root);

    let doc = build_infrastructure_doc(Some(&model))
        .expect("infrastructure doc present for the real workspace model");
    assert!(
        !doc.sections.is_empty(),
        "the real workspace must yield at least one infra section"
    );

    // The binding "cites a real module" gate: every cited adapter module is a
    // `path:line` citation whose path is a real file on disk under the repo root
    // and whose line is within that file. Guards against future renames.
    for section in &doc.sections {
        let (rel_path, line_str) = section.adapter_module.rsplit_once(':').unwrap_or_else(|| {
            panic!(
                "section `{}` adapter module `{}` is not a `path:line` citation",
                section.service, section.adapter_module
            )
        });
        let path = root.join(rel_path);
        assert!(
            path.is_file(),
            "section `{}` cites missing adapter module `{}` (resolved {})",
            section.service,
            section.adapter_module,
            path.display()
        );
        let line: usize = line_str.parse().unwrap_or_else(|_| {
            panic!(
                "section `{}` adapter module `{}` has a non-numeric line `{}`",
                section.service, section.adapter_module, line_str
            )
        });
        assert!(
            line >= 1,
            "section `{}` adapter module `{}` cites line {} (< 1)",
            section.service,
            section.adapter_module,
            line
        );
        let total_lines = std::fs::read_to_string(&path)
            .unwrap_or_else(|err| {
                panic!(
                    "section `{}` adapter module `{}` could not be read ({}): {err}",
                    section.service,
                    section.adapter_module,
                    path.display()
                )
            })
            .lines()
            .count();
        assert!(
            line <= total_lines,
            "section `{}` adapter module `{}` cites line {} beyond file length {}",
            section.service,
            section.adapter_module,
            line,
            total_lines
        );
    }
}

#[test]
fn real_model_includes_feature_gated_datastore_sections() {
    let root = repo_root();
    let model = build_system_model(&root);
    let doc =
        build_infrastructure_doc(Some(&model)).expect("infrastructure doc present for real model");

    // gcode enables postgres/falkor/qdrant/ai, so each feature-gated adapter
    // boundary must surface as a section. Map by the system model's stable
    // service display names.
    for expected in [
        "PostgreSQL hub",
        "FalkorDB graph",
        "Qdrant vectors",
        "Embedding API",
    ] {
        assert!(
            doc.sections.iter().any(|s| s.service == expected),
            "expected an infra section for `{expected}`; sections present: {:?}",
            doc.sections.iter().map(|s| &s.service).collect::<Vec<_>>()
        );
    }
}

#[test]
fn absent_boundary_is_omitted_from_rendered_doc() {
    // A model with ONLY a Postgres boundary must render exactly one section and
    // none of the other services. Constructed directly so the assertion is not
    // coupled to the real workspace's full boundary set.
    let model = SystemModel {
        crates: Vec::new(),
        edges: Vec::new(),
        services: vec![ServiceBoundary {
            name: "PostgreSQL hub".to_string(),
            kind: ServiceKind::Postgres,
            pulled_in_by: vec!["gobby-code (feature: postgres)".to_string()],
        }],
        runtime_modes: vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached],
        features_by_crate: std::collections::BTreeMap::new(),
        notes: Vec::new(),
    };

    let doc = build_infrastructure_doc(Some(&model)).expect("doc present");
    assert_eq!(
        doc.sections.len(),
        1,
        "only the Postgres section is present"
    );
    assert_eq!(doc.sections[0].service, "PostgreSQL hub");

    let rendered = render_infrastructure_doc(&doc);
    assert!(rendered.contains("## PostgreSQL hub"));
    // No section for any boundary kind absent from the model.
    assert!(!rendered.contains("FalkorDB graph"));
    assert!(!rendered.contains("Qdrant vectors"));
    assert!(!rendered.contains("Embedding API"));
    assert!(!rendered.contains("Media toolchain"));
    // The Postgres adapter is still cited (as a `path:line` citation).
    assert!(rendered.contains("crates/gcore/src/postgres.rs:16"));
}

#[test]
fn infrastructure_doc_is_non_degrading_and_omitted_without_a_model() {
    // No model -> no page (mirrors the architecture diagrams).
    assert!(build_infrastructure_doc(None).is_none());

    // A present doc never marks itself degraded.
    let root = repo_root();
    let model = build_system_model(&root);
    let doc = build_infrastructure_doc(Some(&model)).expect("doc present for real model");
    assert!(
        doc.degraded_sources.is_empty(),
        "infra page is deterministic and never degrades: {:?}",
        doc.degraded_sources
    );
}
