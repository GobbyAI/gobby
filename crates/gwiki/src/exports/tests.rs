use std::fs;

use crate::graph::analytics::GraphAnalyticsError;
use crate::graph::{
    GraphExportOptions, WikiGraphCodeEdge, WikiGraphDocument, WikiGraphFacts, WikiGraphLink,
    WikiGraphLinkTarget, WikiGraphSource,
};
use crate::search::SearchScope;

use super::graph::graph_export_error;
use super::write::{StagedExport, commit_staged_exports, rollback_staged_exports};
use super::*;

#[test]
fn exports_do_not_mutate_wiki_state() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let wiki_page = root.join("knowledge/topics/ownership.md");
    fs::create_dir_all(wiki_page.parent().expect("wiki parent")).expect("wiki dir");
    fs::write(&wiki_page, "# Ownership\n\nCanonical page.\n").expect("wiki page");
    let before = fs::read_to_string(&wiki_page).expect("read before");

    let artifact = write_export(
        root,
        ExportRequest {
            filename: "ownership-bundle.md".to_string(),
            kind: ExportKind::Bundle,
            contents: "# Ownership Bundle\n\nGenerated export.\n".to_string(),
        },
    )
    .expect("export is written");

    assert_eq!(artifact.path, root.join("outputs/ownership-bundle.md"));
    assert_eq!(artifact.bytes_written, 38);
    assert_eq!(fs::read_to_string(&wiki_page).expect("read after"), before);
    assert_eq!(
        fs::read_to_string(root.join("outputs/ownership-bundle.md")).expect("export"),
        "# Ownership Bundle\n\nGenerated export.\n"
    );
    assert_eq!(
        bundled_workflow_assets()
            .iter()
            .map(|asset| asset.name)
            .collect::<Vec<_>>(),
        vec!["compile", "query", "audit"]
    );

    let bundle_artifact =
        export_workflow_assets(root, "workflow-assets.md").expect("workflow assets export");
    assert_eq!(
        bundle_artifact.path,
        root.join("outputs/workflow-assets.md")
    );
    let bundle = fs::read_to_string(root.join("outputs/workflow-assets.md")).expect("bundle");
    assert!(bundle.contains("# GWiki Workflow Assets"));
    assert!(bundle.contains("## compile"));

    let report = root.join("meta/health/latest.md");
    fs::create_dir_all(report.parent().expect("report parent")).expect("report dir");
    fs::write(&report, "# Health\n\nGenerated report.\n").expect("report");
    let report_artifact =
        export_report_file(root, "reports/health.md", &report).expect("report export");
    assert_eq!(report_artifact.path, root.join("outputs/reports/health.md"));
    assert_eq!(
        fs::read_to_string(root.join("outputs/reports/health.md")).expect("report export"),
        "# Health\n\nGenerated report.\n"
    );
    assert_eq!(fs::read_to_string(&wiki_page).expect("read final"), before);
}

#[test]
fn graph_analytics_export_artifacts_include_degradation_and_mermaid() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let scope = SearchScope::project("project-123");
    let facts = WikiGraphFacts {
        documents: vec![
            WikiGraphDocument {
                scope: scope.clone(),
                path: "knowledge/topics/overview.md".into(),
                title: Some("Overview".to_string()),
            },
            WikiGraphDocument {
                scope: scope.clone(),
                path: "code/src/lib.rs".into(),
                title: Some("lib.rs".to_string()),
            },
            WikiGraphDocument {
                scope: scope.clone(),
                path: "documents/design.md".into(),
                title: Some("Design Notes".to_string()),
            },
        ],
        links: vec![WikiGraphLink {
            scope: scope.clone(),
            source_path: "knowledge/topics/overview.md".into(),
            raw_target: "code/src/lib.rs".to_string(),
            target: WikiGraphLinkTarget::Resolved("code/src/lib.rs".into()),
        }],
        sources: vec![WikiGraphSource {
            scope,
            source_path: "raw/sources/example.md".into(),
            document_path: "knowledge/topics/overview.md".into(),
        }],
        code_edges: Vec::new(),
    };

    let artifacts = export_graph_artifacts(
        root,
        &facts,
        GraphExportOptions::degraded(vec![
            "falkordb_unavailable".to_string(),
            "semantic_relations_unavailable".to_string(),
        ]),
    )
    .expect("graph artifacts exported");

    assert_eq!(artifacts.len(), 2);
    assert_eq!(artifacts[0].path, root.join("outputs/graph.json"));
    assert_eq!(artifacts[1].path, root.join("outputs/GRAPH_REPORT.md"));

    let graph_json: serde_json::Value = serde_json::from_str(
        &fs::read_to_string(root.join("outputs/graph.json")).expect("graph json"),
    )
    .expect("valid graph json");
    assert_eq!(graph_json["degraded"], true);
    assert_eq!(
        graph_json["degraded_sources"],
        serde_json::json!(["falkordb_unavailable", "semantic_relations_unavailable"])
    );
    assert_eq!(graph_json["nodes"].as_array().expect("nodes").len(), 5);
    assert_eq!(
        graph_json["edges"]["links"]
            .as_array()
            .expect("links")
            .len(),
        1
    );
    assert_eq!(
        graph_json["edges"]["imports"].as_array().expect("imports"),
        &Vec::<serde_json::Value>::new()
    );
    assert_eq!(
        graph_json["edges"]["calls"].as_array().expect("calls"),
        &Vec::<serde_json::Value>::new()
    );
    assert_eq!(
        graph_json["edges"]["trust"]
            .as_array()
            .expect("trust")
            .len(),
        1
    );
    assert_eq!(
        graph_json["edges"]["audit"]
            .as_array()
            .expect("audit")
            .len(),
        1
    );
    let central_node_id = graph_json["analytics"]["centrality"][0]["node"]["id"]
        .as_str()
        .expect("central node id");
    assert!(central_node_id.starts_with("document-knowledge-topics-overview-md-"));
    assert_eq!(
        graph_json["analytics"]["centrality"][0]["degree"],
        serde_json::json!(2)
    );
    // Weighted Leiden clusters the citation→source→overview→lib.rs chain
    // into two 2-node communities (split at the middle "supports" edge),
    // plus the isolated design.md singleton — three communities total. The
    // first (sorted by smallest member id) is the {citation, source} pair.
    assert_eq!(
        graph_json["analytics"]["communities"][0]["nodes"]
            .as_array()
            .expect("community nodes")
            .len(),
        2
    );

    let report = fs::read_to_string(root.join("outputs/GRAPH_REPORT.md")).expect("graph report");
    assert!(report.contains("# GWiki Graph Report"));
    assert!(report.contains("## Analytics"));
    assert!(report.contains(&format!("- Top central node: {central_node_id} (degree 2)")));
    assert!(report.contains("- Communities: 3"));
    assert!(report.contains("## Degraded sources"));
    assert!(report.contains("- falkordb_unavailable"));
    assert!(report.contains("```mermaid"));
    assert!(report.contains(" --> "));
}

#[test]
fn graph_export_removes_json_when_report_write_fails() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let outputs = root.join("outputs");
    fs::create_dir_all(&outputs).expect("outputs dir");
    fs::write(outputs.join("graph.json"), "existing graph").expect("existing graph");
    fs::create_dir(outputs.join("GRAPH_REPORT.md")).expect("blocking dir");

    let error = export_graph_artifacts(
        root,
        &WikiGraphFacts::default(),
        GraphExportOptions::available(),
    )
    .expect_err("report path directory should fail");

    assert!(error.to_string().contains("GRAPH_REPORT.md"));
    assert_eq!(
        fs::read_to_string(outputs.join("graph.json")).expect("preserved graph"),
        "existing graph"
    );
}

#[test]
fn graph_export_errors_are_invalid_input() {
    let error = graph_export_error(GraphAnalyticsError::DuplicateNode {
        id: "node-1".to_string(),
        existing_kind: "topic".to_string(),
        duplicate_kind: "source".to_string(),
        existing_weight: 1.0,
        duplicate_weight: 0.5,
    });

    assert!(matches!(
        error,
        WikiError::InvalidInput {
            field: "graph",
            message
        } if message.contains("duplicate graph node `node-1`")
    ));
}

fn agent_export_facts(scope: SearchScope) -> WikiGraphFacts {
    WikiGraphFacts {
        documents: vec![
            WikiGraphDocument {
                scope: scope.clone(),
                path: "knowledge/topics/overview.md".into(),
                title: Some("Overview".to_string()),
            },
            WikiGraphDocument {
                scope: scope.clone(),
                path: "code/src/lib.rs".into(),
                title: Some("lib.rs".to_string()),
            },
            WikiGraphDocument {
                scope: scope.clone(),
                path: "documents/design.md".into(),
                title: Some("Design Notes".to_string()),
            },
        ],
        links: vec![WikiGraphLink {
            scope: scope.clone(),
            source_path: "knowledge/topics/overview.md".into(),
            raw_target: "[[Design Notes]]".to_string(),
            target: WikiGraphLinkTarget::Resolved("documents/design.md".into()),
        }],
        sources: vec![WikiGraphSource {
            scope: scope.clone(),
            source_path: "raw/sources/example.md".into(),
            document_path: "knowledge/topics/overview.md".into(),
        }],
        code_edges: vec![WikiGraphCodeEdge {
            scope,
            document_path: "code/src/lib.rs".into(),
            source: "alpha".to_string(),
            target: "beta".to_string(),
            kind: "imports".to_string(),
            direction: "out".to_string(),
            line: Some(3),
            provenance: "code-edge-must-be-excluded".to_string(),
        }],
    }
}

#[test]
fn agent_exports_emit_index_jsonld_and_content() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let overview = root.join("knowledge/topics/overview.md");
    fs::create_dir_all(overview.parent().expect("overview parent")).expect("overview dir");
    fs::write(&overview, "# Overview\n\nVault entrypoint.\n").expect("overview page");

    let artifacts = export_agent_artifacts(
        root,
        &agent_export_facts(SearchScope::project("project-123")),
        GraphExportOptions::available(),
    )
    .expect("agent artifacts exported");

    assert_eq!(
        artifacts
            .iter()
            .map(|artifact| artifact.path.clone())
            .collect::<Vec<_>>(),
        vec![
            root.join("outputs/graph.jsonld"),
            root.join("outputs/llms.txt"),
            root.join("outputs/llms-full.txt"),
        ]
    );

    // graph.jsonld: schema.org JSON-LD of the vault document graph.
    let jsonld_text = fs::read_to_string(root.join("outputs/graph.jsonld")).expect("graph jsonld");
    let jsonld: serde_json::Value = serde_json::from_str(&jsonld_text).expect("valid graph jsonld");
    assert_eq!(jsonld["@context"], "https://schema.org");
    let graph = jsonld["@graph"].as_array().expect("@graph array");
    // Vault nodes only: 3 documents + 1 source + 1 citation. No code endpoints.
    assert_eq!(graph.len(), 5);

    let overview_entity = graph
        .iter()
        .find(|entity| entity["url"] == serde_json::json!("knowledge/topics/overview.md"))
        .expect("overview entity");
    assert_eq!(overview_entity["@type"], "Article");
    assert_eq!(overview_entity["genre"], "wiki_page");
    assert_eq!(overview_entity["name"], "Overview");
    // Wikilink → cites the resolved target page.
    let design_id = graph
        .iter()
        .find(|entity| entity["url"] == serde_json::json!("documents/design.md"))
        .expect("design entity")["@id"]
        .clone();
    assert_eq!(
        overview_entity["citation"],
        serde_json::json!([{ "@id": design_id }])
    );
    // Source provenance → isBasedOn the supporting source.
    let source_id = graph
        .iter()
        .find(|entity| entity["genre"] == serde_json::json!("source"))
        .expect("source entity")["@id"]
        .clone();
    assert_eq!(
        overview_entity["isBasedOn"],
        serde_json::json!([{ "@id": source_id }])
    );

    let lib_entity = graph
        .iter()
        .find(|entity| entity["url"] == serde_json::json!("code/src/lib.rs"))
        .expect("lib entity");
    assert_eq!(lib_entity["@type"], "SoftwareSourceCode");

    // The code graph is excluded: no code-edge endpoints or edge classes.
    assert!(!jsonld_text.contains("code-edge-must-be-excluded"));
    assert!(!jsonld_text.contains("\"imports\""));
    assert!(!jsonld_text.contains("\"calls\""));

    // llms.txt: portable index with link sections.
    let index = fs::read_to_string(root.join("outputs/llms.txt")).expect("llms index");
    assert!(index.starts_with("# GWiki Vault Index"));
    assert!(
        index.contains("> Static agent index for project project-123. 3 documents, 1 sources.")
    );
    assert!(index.contains("## Documents"));
    assert!(index.contains("- [Overview](knowledge/topics/overview.md)"));
    assert!(index.contains("- [Design Notes](documents/design.md)"));
    assert!(index.contains("## Sources"));
    assert!(index.contains("- [raw/sources/example.md](raw/sources/example.md)"));

    // llms-full.txt: real content for present docs, placeholder for missing ones.
    let full = fs::read_to_string(root.join("outputs/llms-full.txt")).expect("llms full");
    assert!(full.starts_with("# GWiki Vault Content"));
    assert!(full.contains("## Overview"));
    assert!(full.contains("`knowledge/topics/overview.md`"));
    assert!(full.contains("Vault entrypoint."));
    // design.md was never written to disk, so it degrades gracefully.
    assert!(full.contains("## Design Notes"));
    assert!(full.contains("_(content unavailable)_"));
}

#[test]
fn agent_exports_do_not_mutate_vault() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let overview = root.join("knowledge/topics/overview.md");
    fs::create_dir_all(overview.parent().expect("overview parent")).expect("overview dir");
    fs::write(&overview, "# Overview\n\nVault entrypoint.\n").expect("overview page");
    let before = fs::read_to_string(&overview).expect("read before");

    export_agent_artifacts(
        root,
        &agent_export_facts(SearchScope::project("project-123")),
        GraphExportOptions::available(),
    )
    .expect("agent artifacts exported");

    assert_eq!(fs::read_to_string(&overview).expect("read after"), before);
}

#[test]
fn agent_exports_clean_up_on_write_failure() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let outputs = root.join("outputs");
    fs::create_dir_all(&outputs).expect("outputs dir");
    fs::write(outputs.join("graph.jsonld"), "existing graph").expect("existing graph");
    // A directory at the llms.txt path forces the second write to fail.
    fs::create_dir(outputs.join("llms.txt")).expect("blocking dir");

    let error = export_agent_artifacts(
        root,
        &agent_export_facts(SearchScope::project("project-123")),
        GraphExportOptions::available(),
    )
    .expect_err("blocked llms.txt path should fail");

    assert!(error.to_string().contains("llms.txt"));
    assert_eq!(
        fs::read_to_string(outputs.join("graph.jsonld")).expect("preserved graph"),
        "existing graph"
    );
}

#[test]
fn rollback_removes_only_committed_new_exports() {
    let temp = tempfile::tempdir().expect("tempdir");
    let first_temp = temp.path().join("first.tmp");
    let first_target = temp.path().join("first.md");
    let second_target = temp.path().join("second.md");
    fs::write(&first_temp, "first").expect("first temp");

    let mut staged = vec![
        StagedExport {
            artifact: ExportArtifact {
                path: first_target.clone(),
                kind: ExportKind::Report,
                bytes_written: 5,
            },
            temp_path: first_temp,
            backup_path: None,
            committed: false,
        },
        StagedExport {
            artifact: ExportArtifact {
                path: second_target.clone(),
                kind: ExportKind::Report,
                bytes_written: 6,
            },
            temp_path: temp.path().join("missing.tmp"),
            backup_path: None,
            committed: false,
        },
    ];

    commit_staged_exports(&mut staged).expect_err("missing temp fails second commit");
    assert!(staged[0].committed);
    assert!(!staged[1].committed);
    assert_eq!(
        fs::read_to_string(&first_target).expect("first target"),
        "first"
    );

    fs::write(&second_target, "unrelated").expect("unrelated file");
    rollback_staged_exports(&staged);

    assert!(!first_target.exists());
    assert_eq!(
        fs::read_to_string(&second_target).expect("unrelated preserved"),
        "unrelated"
    );
}

#[cfg(unix)]
#[test]
fn agent_exports_do_not_follow_document_symlink_outside_vault() {
    use std::os::unix::fs::symlink;

    let temp = tempfile::tempdir().expect("tempdir");
    let outside = tempfile::tempdir().expect("outside tempdir");
    let root = temp.path();
    let secret = outside.path().join("secret.md");
    fs::write(&secret, "classified outside vault").expect("secret file");

    let design = root.join("documents/design.md");
    fs::create_dir_all(design.parent().expect("design parent")).expect("design dir");
    symlink(&secret, &design).expect("design symlink");

    export_agent_artifacts(
        root,
        &agent_export_facts(SearchScope::project("project-123")),
        GraphExportOptions::available(),
    )
    .expect("agent artifacts exported");

    let full = fs::read_to_string(root.join("outputs/llms-full.txt")).expect("llms full");
    assert!(full.contains("## Design Notes"));
    assert!(!full.contains("classified outside vault"));
    assert!(full.contains("_(content unavailable)_"));
}
