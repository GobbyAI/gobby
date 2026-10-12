use super::{RunMode, front_door::yaml_bool, optional_string_field, parse_hub_origin};

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
