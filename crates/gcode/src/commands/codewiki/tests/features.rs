use super::*;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// Resolve the workspace repo root from this crate's manifest dir.
/// `CARGO_MANIFEST_DIR` points at `crates/gcode`, so two levels up is the root.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// Parse a pinned contract JSON ourselves and return its declared command-set,
/// so the coverage test compares the catalog against the contract directly
/// rather than against the catalog's own resolver.
fn contract_command_names(repo_root: &Path, crate_dir: &str, contract_file: &str) -> Vec<String> {
    let path = repo_root
        .join("crates")
        .join(crate_dir)
        .join("contract")
        .join(contract_file);
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("contract `{}` must be readable: {err}", path.display()));
    let json: serde_json::Value =
        serde_json::from_str(&raw).expect("pinned contract JSON must parse");
    json["commands"]
        .as_array()
        .expect("contract must have a `commands` array")
        .iter()
        .map(|command| {
            command["name"]
                .as_str()
                .expect("each command must have a string `name`")
                .to_string()
        })
        .collect()
}

fn section<'a>(doc: &'a FeatureCatalogDoc, binary: &str) -> &'a FeatureBinarySection {
    doc.sections
        .iter()
        .find(|section| section.binary == binary)
        .unwrap_or_else(|| {
            panic!(
                "expected a `{binary}` section; present: {:?}",
                doc.sections.iter().map(|s| &s.binary).collect::<Vec<_>>()
            )
        })
}

#[test]
fn catalog_command_set_equals_each_pinned_contract_exactly() {
    let root = repo_root();
    let doc = build_feature_catalog_doc(&root, &[])
        .expect("feature catalog present for the real workspace");

    for (binary, crate_dir, contract_file) in [
        ("gcode", "gcode", "gcode.contract.json"),
        ("gwiki", "gwiki", "gwiki.contract.json"),
    ] {
        let contract: BTreeSet<String> = contract_command_names(&root, crate_dir, contract_file)
            .into_iter()
            .collect();
        let catalog: BTreeSet<String> = section(&doc, binary)
            .entries
            .iter()
            .map(|entry| entry.command.clone())
            .collect();

        // No drift: every contract command is in the catalog (resolver gap or
        // omission fails here), and the catalog invents no command not in the
        // contract. Set equality enforces both directions.
        let missing: Vec<_> = contract.difference(&catalog).collect();
        let extra: Vec<_> = catalog.difference(&contract).collect();
        assert!(
            missing.is_empty() && extra.is_empty(),
            "{binary} catalog drifted from contract: missing={missing:?} extra={extra:?}"
        );
    }
}

#[test]
fn every_catalog_command_is_a_real_contract_command() {
    let root = repo_root();
    let doc = build_feature_catalog_doc(&root, &[]).expect("catalog present");

    for (binary, crate_dir, contract_file) in [
        ("gcode", "gcode", "gcode.contract.json"),
        ("gwiki", "gwiki", "gwiki.contract.json"),
    ] {
        let contract: BTreeSet<String> = contract_command_names(&root, crate_dir, contract_file)
            .into_iter()
            .collect();
        for entry in &section(&doc, binary).entries {
            assert!(
                contract.contains(&entry.command),
                "{binary} catalog invented command `{}` absent from the contract",
                entry.command
            );
        }
    }
}

#[test]
fn every_entry_resolves_to_a_handler_file_that_exists_on_disk() {
    let root = repo_root();
    let doc = build_feature_catalog_doc(&root, &[]).expect("catalog present");

    for binary_section in &doc.sections {
        for entry in &binary_section.entries {
            // The catalog must be completely resolved: every gcode/gwiki
            // command maps to a handler file (no empty `handler_file` rows).
            assert!(
                !entry.handler_file.is_empty(),
                "{} command `{}` has no resolved handler file",
                binary_section.binary,
                entry.command
            );
            let path = root.join(&entry.handler_file);
            assert!(
                path.is_file(),
                "{} command `{}` cites missing handler file `{}` (resolved {})",
                binary_section.binary,
                entry.command,
                entry.handler_file,
                path.display()
            );
            assert!(
                !entry.entry_symbol.is_empty(),
                "{} command `{}` has no entry symbol",
                binary_section.binary,
                entry.command
            );
        }
    }
}

#[test]
fn catalog_is_non_degrading_and_absent_without_any_contract() {
    let root = repo_root();
    let doc = build_feature_catalog_doc(&root, &[]).expect("catalog present for real workspace");
    assert!(
        doc.degraded_sources.is_empty(),
        "feature catalog is deterministic and never degrades: {:?}",
        doc.degraded_sources
    );

    // A directory with no contract files yields no catalog at all.
    let empty = tempfile::tempdir().expect("temp dir");
    assert!(
        build_feature_catalog_doc(empty.path(), &[]).is_none(),
        "no contracts -> no catalog page"
    );
}

#[test]
fn rendered_catalog_names_a_known_command_and_links_to_a_source_page() {
    let root = repo_root();
    let doc = build_feature_catalog_doc(&root, &[]).expect("catalog present");
    let rendered = render_feature_catalog_doc(&doc);

    // Section headings for the two contract-backed binaries plus the ghook
    // prose subsection.
    assert!(rendered.contains("## gcode"));
    assert!(rendered.contains("## gwiki"));
    assert!(rendered.contains("## ghook"));
    // A known command surfaces with its handler entry symbol.
    assert!(
        rendered.contains("`search`"),
        "rendered catalog should name the `search` command"
    );
    assert!(rendered.contains("commands::search::search"));
    // Handler files render as code/files wikilinks.
    assert!(
        rendered.contains("[[code/files/"),
        "rendered catalog should link handler files as wikilinks"
    );
    // Deterministic page: derived from contracts, no LLM.
    assert!(rendered.contains("derived deterministically from the pinned CLI contracts"));
}
