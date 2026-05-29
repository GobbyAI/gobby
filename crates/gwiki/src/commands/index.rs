use std::path::PathBuf;

use serde_json::json;

use crate::CommandOutcome;
use crate::ScopeIdentity;

pub fn run(scope: ScopeIdentity) -> CommandOutcome {
    let payload = json!({
        "command": "index",
        "scope": scope,
        "status": "ready",
        "indexed": {
            "documents": 0,
            "chunks": 0,
            "links": 0,
        },
    });
    let text = format!("Index ready\nScope: {scope}\nDocuments: 0\nChunks: 0\nLinks: 0");
    super::scoped_outcome("index", &scope, payload, text)
}

pub fn ingest_file(path: PathBuf, scope: ScopeIdentity) -> CommandOutcome {
    let display_path = path.to_string_lossy().to_string();
    let payload = json!({
        "command": "ingest-file",
        "scope": scope,
        "status": "ready",
        "path": display_path,
    });
    let text = format!("Ingest file ready\nScope: {scope}\nPath: {display_path}");
    super::scoped_outcome("ingest-file", &scope, payload, text)
}
