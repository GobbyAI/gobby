//! Renders resolved grants as SRT's settings schema (`srt_settings.py`), and
//! serializes them with sorted keys and no spaces, as the oracle hashes them.

use super::{
    paths::{ResolvedPaths, expand_user, resolve_nonstrict, subtree_prefix, text},
    tables::SRT_DEFAULT_WRITE_PATHS,
};
use serde_json::{Value, json};
use std::path::Path;

/// Harmless violations SRT should not record. Enforcement is unchanged.
const IGNORED_VIOLATIONS: [&str; 8] = [
    "sysctl-read kern.iossupportversion",
    "system-info vfs.disk-space",
    "mach-lookup com.apple.SystemConfiguration.configd",
    "system-info net.link.addr",
    "mach-lookup com.apple.FSEvents",
    "mach-lookup com.apple.DiskArbitration.diskarbitrationd",
    "sysctl-read hw.optional.",
    "sysctl-read hw.cpusubfamily",
];

/// `render_srt_settings`: the SRT settings document for one resolved policy.
pub(super) fn render(paths: &ResolvedPaths, home: &Path) -> Value {
    let credentials: Vec<Value> = paths
        .credential_env_vars
        .iter()
        .map(|credential| {
            json!({
                "name": credential.name,
                "mode": "mask",
                "injectHosts": credential.inject_hosts,
            })
        })
        .collect();
    let mut network = json!({
        "allowedDomains": paths.allowed_domains,
        "deniedDomains": paths.denied_domains,
        "strictAllowlist": true,
        "allowUnixSockets": paths.allow_unix_sockets,
        "allowAllUnixSockets": false,
        // Loopback egress: ghook, gmcp and gcode reach the daemon and hub directly.
        "allowLocalBinding": true,
    });
    if !credentials.is_empty() {
        network["tlsTerminate"] = json!({});
    }
    if paths.provider == "grok" {
        // Grok queries SkyLight flags on its UI thread, including after compact.
        network["allowMachLookup"] = json!(["com.apple.windowserver.active"]);
    }
    let mut settings = json!({
        "network": network,
        "filesystem": {
            "denyRead": paths.deny_read_paths,
            "allowRead": paths.read_paths,
            "allowWrite": paths.write_paths,
            "denyWrite": effective_deny_write(&paths.write_paths, &paths.deny_write_paths, home),
            "allowGitConfig": false,
        },
        "allowPty": true,
        "enableWeakerNestedSandbox": false,
        // Adds only the com.apple.trustd.agent lookup that native TLS verification needs.
        "enableWeakerNetworkIsolation": true,
        "allowAppleEvents": false,
        "ignoreViolations": { "*": IGNORED_VIOLATIONS },
    });
    if !credentials.is_empty() {
        settings["credentials"] = json!({
            "envVars": credentials,
            "allowPlaintextInject": false,
        });
    }
    settings
}

/// `_effective_deny_write`: keep only the write denies that share a subtree with a grant.
/// Writes outside every grant are already denied, and seatbelt compiles each entry.
fn effective_deny_write(allow_write: &[String], deny_write: &[String], home: &Path) -> Vec<String> {
    let defaults: Vec<String> = SRT_DEFAULT_WRITE_PATHS
        .iter()
        .map(|path| text(&expand_user(path, home)))
        .collect();
    // SRT grants each default by its realpath as well as its literal spelling.
    let real: Vec<String> = defaults
        .iter()
        .map(|path| text(&resolve_nonstrict(Path::new(path))))
        .collect();
    let grants: Vec<String> = defaults
        .iter()
        .chain(&real)
        .chain(allow_write)
        .map(|path| subtree_prefix(path))
        .collect();
    deny_write
        .iter()
        .filter(|deny| {
            let prefix = subtree_prefix(deny);
            grants
                .iter()
                .any(|grant| prefix.starts_with(grant) || grant.starts_with(&prefix))
        })
        .cloned()
        .collect()
}

/// Serialize with sorted object keys and no whitespace, matching `json.dumps(sort_keys=True)`.
pub(super) fn canonical_json(value: &Value) -> String {
    match value {
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|left, right| left.0.cmp(right.0));
            let fields: Vec<String> = entries
                .into_iter()
                .map(|(key, item)| {
                    format!("{}:{}", Value::String(key.clone()), canonical_json(item))
                })
                .collect();
            format!("{{{}}}", fields.join(","))
        }
        Value::Array(items) => {
            let items: Vec<String> = items.iter().map(canonical_json).collect();
            format!("[{}]", items.join(","))
        }
        scalar => scalar.to_string(),
    }
}
