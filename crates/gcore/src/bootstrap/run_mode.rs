use super::{FilesHomeError, RunMode, front_door::yaml_bool};

pub(super) const LOCAL_HUB_URL_REFUSAL: &str =
    "hub_daemon_url is not allowed on a local bootstrap; this process is the owner";

pub(super) fn configured_value(map: &serde_yaml::Mapping, name: &str) -> bool {
    map.get(name).is_some_and(|value| {
        !value.is_null() && value.as_str().is_none_or(|text| !text.trim().is_empty())
    })
}

pub(super) fn hub_daemon_url(map: &serde_yaml::Mapping) -> anyhow::Result<Option<String>> {
    if !configured_value(map, "hub_daemon_url") {
        return Ok(None);
    }
    let text = map["hub_daemon_url"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("hub_daemon_url must be a string"))?;
    Ok(Some(text.trim().to_owned()))
}

pub(super) fn hub_flag(map: &serde_yaml::Mapping) -> anyhow::Result<bool> {
    match map.get("hub") {
        None => Ok(false),
        Some(value) => yaml_bool(value).ok_or_else(|| anyhow::anyhow!("hub must be a boolean")),
    }
}

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
    let hub = hub_flag(map)?;
    if !remote {
        if configured_value(map, "hub_daemon_url") {
            anyhow::bail!(LOCAL_HUB_URL_REFUSAL);
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
    if configured_value(map, "files_home") {
        anyhow::bail!("files_home is not allowed on a remote bootstrap");
    }
    let raw = hub_daemon_url(map)?
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
    let invalid_port = || {
        FilesHomeError::Invalid(
            "hub_daemon_url port must be an integer between 0 and 65535".to_owned(),
        )
    };
    let value = value.replace(['\t', '\r', '\n'], "");
    let (scheme, rest) = value
        // Python str.strip() also treats the four ASCII separators as whitespace.
        .trim_matches(|character: char| {
            character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
        })
        .trim_start_matches(|character: char| character <= '\u{20}')
        .split_once("://")
        .ok_or_else(invalid)?;
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
    if !netloc.starts_with('[') && authority.contains(['[', ']']) {
        return Err(invalid());
    }
    let lowercase_host = |host: &str| {
        host.split_once('%').map_or_else(
            || host.to_lowercase(),
            |(address, scope)| format!("{}%{scope}", address.to_lowercase()),
        )
    };
    let (host, port) = if let Some(ipv6) = netloc.strip_prefix('[') {
        let (host, suffix) = ipv6.split_once(']').ok_or_else(invalid)?;
        // Match urllib's bracketed-host validation, including scoped IPv6 and IPvFuture.
        let valid_host = if let Some(future) = host.strip_prefix('v') {
            future.split_once('.').is_some_and(|(version, address)| {
                !version.is_empty()
                    && version.bytes().all(|byte| byte.is_ascii_hexdigit())
                    && !address.is_empty()
            })
        } else {
            let (address, scope) = host
                .split_once('%')
                .map_or((host, None), |(address, scope)| (address, Some(scope)));
            address.parse::<std::net::Ipv6Addr>().is_ok()
                && scope.is_none_or(|scope| !scope.is_empty() && !scope.contains('%'))
        };
        if !valid_host {
            return Err(invalid());
        }
        let port = if suffix.is_empty() {
            None
        } else {
            Some(suffix.strip_prefix(':').ok_or_else(invalid)?)
        };
        (format!("[{}]", lowercase_host(host)), port)
    } else {
        let (host, port) = netloc
            .split_once(':')
            .map_or((netloc, None), |(host, port)| (host, Some(port)));
        if host.is_empty() || host.contains([':', '[', ']']) {
            return Err(invalid());
        }
        (lowercase_host(host), port)
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
            if !port.bytes().all(|byte| byte.is_ascii_digit()) {
                return Err(invalid_port());
            }
            // Leading zeros do not affect the numeric range.
            let digits = port.trim_start_matches('0');
            let port: u16 = if digits.is_empty() {
                0
            } else {
                digits.parse().map_err(|_| invalid_port())?
            };
            Ok(format!("{scheme}://{host}:{port}"))
        }
        None => Ok(format!("{scheme}://{host}")),
    }
}
