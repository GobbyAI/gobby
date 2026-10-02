//! The `front_door` bootstrap block, parsed identically by
//! `src/gobby/config/bootstrap.py`.
//!
//! A loopback `bind_host` takes any block. A non-loopback one needs the front
//! door enabled (a disabled front door binds Python on the public ports, so a
//! TLS block would secure nothing) and `tls.mode` set to `self-signed` or
//! `files` (the front door serves plaintext only to loopback peers).

use std::collections::BTreeMap;
use std::net::IpAddr;

const FRONT_DOOR_KEYS: [&str; 3] = ["enabled", "routes", "tls"];

const TLS_KEYS: [&str; 4] = ["mode", "cert", "key", "sans"];

/// Certificate path used by `self-signed` and `files` modes unless `tls.cert` is set.
pub const DEFAULT_FRONT_DOOR_CERT: &str = "~/.gobby/tls/front_door.crt";

/// Private key path used by `self-signed` and `files` modes unless `tls.key` is set.
pub const DEFAULT_FRONT_DOOR_KEY: &str = "~/.gobby/tls/front_door.key";

/// How the front door serves one route family.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouteBackend {
    Proxy,
    Native,
    Compare,
}

impl RouteBackend {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "proxy" => Some(Self::Proxy),
            "native" => Some(Self::Native),
            "compare" => Some(Self::Compare),
            _ => None,
        }
    }
}

/// Where the front door's certificate comes from.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum TlsMode {
    /// Plaintext; only permitted on a loopback `bind_host`.
    #[default]
    Off,
    /// Generated once by gdaemon at `cert`/`key`, then reused.
    SelfSigned,
    /// An operator-supplied PEM pair at `cert`/`key`.
    Files,
}

impl TlsMode {
    fn parse(value: &serde_yaml::Value) -> Option<Self> {
        // An unquoted YAML 1.1 `off` is boolean false to PyYAML.
        if value.as_bool() == Some(false) {
            return Some(Self::Off);
        }
        match value.as_str()? {
            "off" => Some(Self::Off),
            "self-signed" => Some(Self::SelfSigned),
            "files" => Some(Self::Files),
            _ => None,
        }
    }
}

/// The `front_door.tls` block. `cert` and `key` are written as configured;
/// gdaemon expands a leading `~` when it opens them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TlsBootstrap {
    pub mode: TlsMode,
    pub cert: String,
    pub key: String,
    /// Extra DNS names or IP literals for a generated certificate.
    pub sans: Vec<String>,
}

impl Default for TlsBootstrap {
    fn default() -> Self {
        Self {
            mode: TlsMode::Off,
            cert: DEFAULT_FRONT_DOOR_CERT.to_owned(),
            key: DEFAULT_FRONT_DOOR_KEY.to_owned(),
            sans: Vec::new(),
        }
    }
}

/// The `front_door` bootstrap block. Families absent from `routes` are proxied.
/// Unknown family names are accepted because Stage 2 leaves introduce families.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrontDoorBootstrap {
    pub enabled: bool,
    pub routes: BTreeMap<String, RouteBackend>,
    pub tls: TlsBootstrap,
}

impl Default for FrontDoorBootstrap {
    fn default() -> Self {
        Self {
            enabled: true,
            routes: BTreeMap::new(),
            tls: TlsBootstrap::default(),
        }
    }
}

/// True for `localhost` or a loopback IP literal, bracketed or not. Mirrors
/// `gobby.config.bootstrap.is_loopback_host`.
pub fn is_loopback_host(host: &str) -> bool {
    let host = host.trim();
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    let bare = host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host);
    bare.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

/// Parse a boolean the same way `src/gobby/config/bootstrap.py` does. PyYAML
/// resolves plain YAML 1.1 words such as `yes` to bool while serde_yaml keeps
/// them as strings, so both parsers accept a bool or one of these words.
fn yaml_bool(value: &serde_yaml::Value) -> Option<bool> {
    if let Some(flag) = value.as_bool() {
        return Some(flag);
    }
    match value.as_str()?.trim().to_ascii_lowercase().as_str() {
        "true" | "yes" | "on" => Some(true),
        "false" | "no" | "off" => Some(false),
        _ => None,
    }
}

pub(super) fn parse_front_door(
    value: Option<&serde_yaml::Value>,
    bind_host: &str,
) -> anyhow::Result<FrontDoorBootstrap> {
    let front_door = parse_front_door_block(value)?;
    if is_loopback_host(bind_host) {
        return Ok(front_door);
    }
    if !front_door.enabled {
        anyhow::bail!(
            "front_door.enabled: false requires a loopback bind_host (got '{bind_host}'); \
             set bind_host to a loopback address, or enable the front door"
        );
    }
    if front_door.tls.mode == TlsMode::Off {
        anyhow::bail!(
            "front_door.tls.mode must be self-signed or files when bind_host '{bind_host}' \
             is not loopback"
        );
    }
    Ok(front_door)
}

fn parse_front_door_block(value: Option<&serde_yaml::Value>) -> anyhow::Result<FrontDoorBootstrap> {
    let Some(value) = value.filter(|v| !v.is_null()) else {
        return Ok(FrontDoorBootstrap::default());
    };
    let Some(map) = value.as_mapping() else {
        anyhow::bail!("bootstrap.yaml field `front_door` must be a mapping");
    };
    reject_unknown_keys(map, &FRONT_DOOR_KEYS, "front_door")?;
    let enabled = match value.get("enabled") {
        None => true,
        Some(v) => {
            yaml_bool(v).ok_or_else(|| anyhow::anyhow!("front_door.enabled must be a boolean"))?
        }
    };
    let mut routes = BTreeMap::new();
    match value.get("routes") {
        None | Some(serde_yaml::Value::Null) => {}
        Some(serde_yaml::Value::Mapping(entries)) => {
            for (family, backend) in entries {
                let family = family.as_str().filter(|f| !f.is_empty()).ok_or_else(|| {
                    anyhow::anyhow!("front_door.routes keys must be non-empty strings")
                })?;
                let backend = backend
                    .as_str()
                    .and_then(RouteBackend::parse)
                    .ok_or_else(|| {
                        anyhow::anyhow!(
                            "front_door.routes.{family} must be one of: proxy, native, compare"
                        )
                    })?;
                routes.insert(family.to_owned(), backend);
            }
        }
        Some(_) => anyhow::bail!("front_door.routes must be a mapping"),
    }
    Ok(FrontDoorBootstrap {
        enabled,
        routes,
        tls: parse_tls(value.get("tls"))?,
    })
}

fn parse_tls(value: Option<&serde_yaml::Value>) -> anyhow::Result<TlsBootstrap> {
    let Some(value) = value.filter(|v| !v.is_null()) else {
        return Ok(TlsBootstrap::default());
    };
    let Some(map) = value.as_mapping() else {
        anyhow::bail!("front_door.tls must be a mapping");
    };
    reject_unknown_keys(map, &TLS_KEYS, "front_door.tls")?;
    let mode = match value.get("mode") {
        None => TlsMode::Off,
        Some(v) => TlsMode::parse(v).ok_or_else(|| {
            anyhow::anyhow!("front_door.tls.mode must be one of: off, self-signed, files")
        })?,
    };
    Ok(TlsBootstrap {
        mode,
        cert: tls_path(value, "cert", DEFAULT_FRONT_DOOR_CERT)?,
        key: tls_path(value, "key", DEFAULT_FRONT_DOOR_KEY)?,
        sans: parse_sans(value.get("sans"))?,
    })
}

fn tls_path(tls: &serde_yaml::Value, field: &str, default: &str) -> anyhow::Result<String> {
    match tls.get(field) {
        None => Ok(default.to_owned()),
        Some(v) => v
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| anyhow::anyhow!("front_door.tls.{field} must be a string")),
    }
}

fn parse_sans(value: Option<&serde_yaml::Value>) -> anyhow::Result<Vec<String>> {
    let entries = match value {
        None | Some(serde_yaml::Value::Null) => return Ok(Vec::new()),
        Some(serde_yaml::Value::Sequence(entries)) => entries,
        Some(_) => {
            anyhow::bail!("front_door.tls.sans must be a list of DNS names or IP literals")
        }
    };
    entries
        .iter()
        .map(|entry| {
            let name = entry
                .as_str()
                .map(str::trim)
                .filter(|name| !name.is_empty())
                .ok_or_else(|| {
                    anyhow::anyhow!("front_door.tls.sans entries must be non-empty strings")
                })?;
            if name.parse::<IpAddr>().is_ok_and(|ip| ip.is_unspecified()) {
                anyhow::bail!("front_door.tls.sans cannot name the unspecified address {name}");
            }
            Ok(name.to_owned())
        })
        .collect()
}

fn reject_unknown_keys(
    map: &serde_yaml::Mapping,
    allowed: &[&str],
    block: &str,
) -> anyhow::Result<()> {
    let mut unknown: Vec<String> = map
        .keys()
        .filter(|key| !key.as_str().is_some_and(|k| allowed.contains(&k)))
        .map(|key| {
            key.as_str()
                .map_or_else(|| format!("{key:?}"), str::to_owned)
        })
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    unknown.sort();
    anyhow::bail!(
        "{block} has unknown keys: {} (allowed: {})",
        unknown.join(", "),
        allowed.join(", ")
    )
}
