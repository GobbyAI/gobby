use std::fs;
use std::path::{Path, PathBuf};

const ROUTING_FLAGS: &[&str] = &[
    "--ai",
    "--transcription-routing",
    "--vision-routing",
    "--text-routing",
    "--require-ai",
];

const PROBE_NEEDLES: &[&str] = &[
    "probe_daemon_capabilities",
    "ProbeObservation",
    "/api/llm/vision/status",
    "/api/voice/status",
];

fn rust_sources(root: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    visit(root, &mut files);
    files
}

fn visit(dir: &Path, files: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).expect("read dir") {
        let entry = entry.expect("dir entry");
        let path = entry.path();
        if path.is_dir() {
            visit(&path, files);
        } else if path.extension().and_then(|ext| ext.to_str()) == Some("rs") {
            files.push(path);
        }
    }
}

#[test]
fn gwiki_contract_exposes_no_routing_flags_beyond_no_ai() {
    let contract = gobby_wiki::contract::contract();
    let mut unexpected = Vec::new();
    let mut saw_no_ai = false;
    for command in &contract.commands {
        for flag in &command.flags {
            if flag.name == "--no-ai" {
                saw_no_ai = true;
            }
            if ROUTING_FLAGS.contains(&flag.name) {
                unexpected.push(format!("{} {}", command.name, flag.name));
            }
        }
    }
    assert!(
        unexpected.is_empty(),
        "routing flags remain in the in-memory contract: {}",
        unexpected.join(", ")
    );
    assert!(saw_no_ai, "contract must keep --no-ai");
}

#[test]
fn gwiki_has_no_probe_or_status_route_body_parsing() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut hits = Vec::new();
    for path in rust_sources(&root) {
        let source = fs::read_to_string(&path).expect("read rust source");
        for needle in PROBE_NEEDLES {
            if source.contains(needle) {
                hits.push(format!("{}: {needle}", path.display()));
            }
        }
    }
    assert!(
        hits.is_empty(),
        "probe / status-route body parsing remains:\n{}",
        hits.join("\n")
    );
}

#[test]
fn ai_configuration_guide_describes_daemon_only_routing_and_outage() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../docs/guides/ai-configuration.md");
    let docs = fs::read_to_string(&path).expect("ai-configuration.md");
    assert!(
        docs.contains("daemon-only AI routing"),
        "guide must name the daemon-only AI routing contract"
    );
    assert!(
        docs.to_ascii_lowercase().contains("outage"),
        "guide must describe outage semantics"
    );
    for retired in ROUTING_FLAGS {
        assert!(
            !contains_flag(&docs, retired),
            "guide still documents retired flag {retired}"
        );
    }
    assert!(
        contains_flag(&docs, "--no-ai"),
        "guide must document --no-ai as the opt-out"
    );
}

fn contains_flag(text: &str, flag: &str) -> bool {
    text.split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '-'))
        .any(|token| token == flag)
}
