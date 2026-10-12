use super::{FilesHomeError, RunMode, front_door::yaml_bool, optional_string_field};

pub(super) fn parse_mode_fields(
    map: &serde_yaml::Mapping,
    bind_host: &str,
    daemon_port: u16,
    daemon_url: Option<&str>,
) -> anyhow::Result<(RunMode, Option<String>)> {
    let remote = match map.get("datastore_mode") {
        None => false,
        Some(value) if value.as_str() == Some("local") => false,
        Some(value) if value.as_str() == Some("remote") => true,
        _ => anyhow::bail!("datastore_mode must be one of: local, remote"),
    };
    let hub = match map.get("hub") {
        None => false,
        Some(value) => yaml_bool(value).ok_or_else(|| anyhow::anyhow!("hub must be a boolean"))?,
    };
    let hub_daemon_url = optional_string_field(map, "hub_daemon_url")?;
    if !remote {
        if hub_daemon_url.is_some() {
            anyhow::bail!(
                "hub_daemon_url is not allowed on a local bootstrap; this process is the owner"
            );
        }
        return Ok((
            if hub {
                RunMode::Hub
            } else {
                RunMode::Standalone
            },
            None,
        ));
    }
    if hub {
        anyhow::bail!("hub: true requires datastore_mode: local");
    }
    let raw = hub_daemon_url
        .ok_or_else(|| anyhow::anyhow!("hub_daemon_url is required for datastore_mode: remote"))?;
    let origin = parse_hub_origin(&raw)?;
    let own_origins = [
        Some(format!("http://{bind_host}:{daemon_port}")),
        Some(format!("https://{bind_host}:{daemon_port}")),
        daemon_url.map(str::to_owned),
    ];
    if own_origins
        .iter()
        .flatten()
        .any(|own| parse_hub_origin(own).is_ok_and(|own_origin| own_origin == origin))
    {
        anyhow::bail!("hub_daemon_url must not be this process's own origin");
    }
    Ok((RunMode::Node, Some(origin)))
}

pub(super) fn parse_hub_origin(value: &str) -> Result<String, FilesHomeError> {
    let invalid = || {
        FilesHomeError::Invalid("hub_daemon_url must be an http(s) origin with a host".to_owned())
    };
    let (scheme, rest) = value.trim().split_once("://").ok_or_else(invalid)?;
    let scheme = scheme.to_ascii_lowercase();
    if !matches!(scheme.as_str(), "http" | "https") {
        return Err(invalid());
    }
    let (rest, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (rest, query) = rest.split_once('?').unwrap_or((rest, ""));
    let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
    let netloc = authority
        .rsplit_once('@')
        .map_or(authority, |(_, host)| host);
    let (host, port) = if let Some(ipv6) = netloc.strip_prefix('[') {
        let (host, suffix) = ipv6.split_once(']').ok_or_else(invalid)?;
        if host.is_empty() {
            return Err(invalid());
        }
        let port = if suffix.is_empty() {
            None
        } else {
            Some(suffix.strip_prefix(':').ok_or_else(invalid)?)
        };
        (format!("[{}]", host.to_lowercase()), port)
    } else {
        let (host, port) = netloc
            .rsplit_once(':')
            .map_or((netloc, None), |(host, port)| (host, Some(port)));
        if host.is_empty() || host.contains([':', '[', ']']) {
            return Err(invalid());
        }
        (host.to_lowercase(), port)
    };
    if authority.contains('@') {
        return Err(FilesHomeError::Invalid(
            "hub_daemon_url must not include userinfo".to_owned(),
        ));
    }
    if !query.is_empty() || !fragment.is_empty() {
        return Err(FilesHomeError::Invalid(
            "hub_daemon_url must not include a query or fragment".to_owned(),
        ));
    }
    if !path.is_empty() {
        return Err(FilesHomeError::Invalid(
            "hub_daemon_url path must be empty or /".to_owned(),
        ));
    }
    match port.filter(|port| !port.is_empty()) {
        Some(port) => {
            let port: u16 = port.parse().map_err(|_| invalid())?;
            Ok(format!("{scheme}://{host}:{port}"))
        }
        None => Ok(format!("{scheme}://{host}")),
    }
}
