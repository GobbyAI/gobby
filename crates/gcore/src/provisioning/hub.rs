use super::*;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnsureHubOptions {
    pub gobby_home: PathBuf,
    pub service_options: DockerServiceOptions,
    pub candidate_database_urls: Vec<String>,
    pub provision_services: bool,
}

impl EnsureHubOptions {
    pub fn new(gobby_home: PathBuf) -> Self {
        Self {
            service_options: DockerServiceOptions::new(gobby_home.clone()),
            gobby_home,
            candidate_database_urls: Vec::new(),
            provision_services: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HubIdentity {
    pub system_identifier: String,
    pub database_name: String,
}

impl HubIdentity {
    fn display_label(&self) -> String {
        format!(
            "system_identifier={}, database={}",
            self.system_identifier, self.database_name
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HubIdentityProbeResult {
    Known(HubIdentity),
    UnknownInsufficientPrivilege { message: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecordedHubIdentityStatus {
    SingleReachable,
    VerifiedSameHub,
    IdentityUnknownInsufficientPrivilege { message: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordedHubResolution {
    pub database_url: String,
    pub identity_status: RecordedHubIdentityStatus,
}

pub fn ensure_hub(
    options: &EnsureHubOptions,
) -> anyhow::Result<(String, Option<DockerProvisioningReport>)> {
    ensure_hub_with_identity(
        options,
        |name| std::env::var(name).ok(),
        postgres_database_reachable,
        probe_postgres_hub_identity,
        provision_docker_services,
    )
}

#[cfg(test)]
pub(crate) fn ensure_hub_with(
    options: &EnsureHubOptions,
    get_env: impl FnMut(&str) -> Option<String>,
    database_reachable: impl FnMut(&str) -> bool,
    provision: impl FnOnce(&DockerServiceOptions) -> anyhow::Result<DockerProvisioningReport>,
) -> anyhow::Result<(String, Option<DockerProvisioningReport>)> {
    ensure_hub_with_identity(
        options,
        get_env,
        database_reachable,
        |_| {
            Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                message: "identity_unknown_insufficient_privilege: test probe not configured"
                    .to_string(),
            })
        },
        provision,
    )
}

pub(crate) fn ensure_hub_with_identity(
    options: &EnsureHubOptions,
    mut get_env: impl FnMut(&str) -> Option<String>,
    mut database_reachable: impl FnMut(&str) -> bool,
    mut identity_probe: impl FnMut(&str) -> anyhow::Result<HubIdentityProbeResult>,
    provision: impl FnOnce(&DockerServiceOptions) -> anyhow::Result<DockerProvisioningReport>,
) -> anyhow::Result<(String, Option<DockerProvisioningReport>)> {
    let mut override_database_url = None;
    let mut gcore_database_url = None;
    let mut bootstrap_database_url = None;

    for candidate in resolve_hub_database_urls(options, &mut get_env)? {
        match candidate.source {
            HubDatabaseUrlSource::Candidate | HubDatabaseUrlSource::Env => {
                if override_database_url.is_none()
                    && explicit_database_url_reachable(
                        &candidate.database_url,
                        &mut database_reachable,
                    )
                {
                    override_database_url = Some(candidate.database_url);
                }
            }
            HubDatabaseUrlSource::GcoreConfig => {
                gcore_database_url = Some(candidate.database_url);
            }
            HubDatabaseUrlSource::Bootstrap => {
                bootstrap_database_url = Some(candidate.database_url);
            }
        }
    }

    let recorded_resolution = resolve_recorded_hub_database_url(
        gcore_database_url.as_deref(),
        bootstrap_database_url.as_deref(),
        &mut database_reachable,
        &mut identity_probe,
    )?;

    if let Some(override_database_url) = override_database_url {
        if let Some(recorded) = recorded_resolution
            && let Some(resolution) = resolve_recorded_hub_database_url(
                Some(&recorded.database_url),
                Some(&override_database_url),
                &mut database_reachable,
                &mut identity_probe,
            )?
        {
            if let RecordedHubIdentityStatus::IdentityUnknownInsufficientPrivilege { message } =
                &resolution.identity_status
            {
                log::warn!("{message}");
            }
            return Ok((resolution.database_url, None));
        }
        return Ok((override_database_url, None));
    }

    if let Some(resolution) = recorded_resolution {
        if let RecordedHubIdentityStatus::IdentityUnknownInsufficientPrivilege { message } =
            &resolution.identity_status
        {
            log::warn!("{message}");
        }
        return Ok((resolution.database_url, None));
    }

    if !options.provision_services {
        anyhow::bail!(
            "no reachable Gobby PostgreSQL hub found and service provisioning is disabled"
        );
    }

    let report = provision(&options.service_options).context("failed to provision Gobby hub")?;
    Ok((
        default_database_url(options.service_options.postgres_port),
        Some(report),
    ))
}

pub fn resolve_recorded_hub_database_url(
    existing_database_url: Option<&str>,
    daemon_database_url: Option<&str>,
    mut database_reachable: impl FnMut(&str) -> bool,
    mut identity_probe: impl FnMut(&str) -> anyhow::Result<HubIdentityProbeResult>,
) -> anyhow::Result<Option<RecordedHubResolution>> {
    let existing_database_url =
        existing_database_url.and_then(|value| non_empty_trimmed(Some(value.to_string())));
    let daemon_database_url =
        daemon_database_url.and_then(|value| non_empty_trimmed(Some(value.to_string())));

    match (existing_database_url, daemon_database_url) {
        (None, None) => Ok(None),
        (Some(existing), None) => {
            if database_reachable(&existing) {
                Ok(Some(RecordedHubResolution {
                    database_url: existing,
                    identity_status: RecordedHubIdentityStatus::SingleReachable,
                }))
            } else {
                Ok(None)
            }
        }
        (None, Some(daemon)) => {
            if database_reachable(&daemon) {
                Ok(Some(RecordedHubResolution {
                    database_url: daemon,
                    identity_status: RecordedHubIdentityStatus::SingleReachable,
                }))
            } else {
                Ok(None)
            }
        }
        (Some(existing), Some(daemon)) if existing == daemon => {
            if database_reachable(&daemon) {
                Ok(Some(RecordedHubResolution {
                    database_url: daemon,
                    identity_status: RecordedHubIdentityStatus::VerifiedSameHub,
                }))
            } else {
                Ok(None)
            }
        }
        (Some(existing), Some(daemon)) => {
            let existing_reachable = database_reachable(&existing);
            let daemon_reachable = database_reachable(&daemon);

            match (existing_reachable, daemon_reachable) {
                (false, false) => Ok(None),
                (true, false) => Ok(Some(RecordedHubResolution {
                    database_url: existing,
                    identity_status: RecordedHubIdentityStatus::SingleReachable,
                })),
                (false, true) => Ok(Some(RecordedHubResolution {
                    database_url: daemon,
                    identity_status: RecordedHubIdentityStatus::SingleReachable,
                })),
                (true, true) => {
                    let existing_redacted = redacted_postgres_dsn_placeholder("existing");
                    let daemon_redacted = redacted_postgres_dsn_placeholder("daemon");
                    let existing_identity = identity_probe(&existing).with_context(|| {
                        format!("failed to probe PostgreSQL hub identity for {existing_redacted}")
                    })?;
                    let daemon_identity = identity_probe(&daemon).with_context(|| {
                        format!("failed to probe PostgreSQL hub identity for {daemon_redacted}")
                    })?;

                    match (existing_identity, daemon_identity) {
                        (
                            HubIdentityProbeResult::Known(existing_identity),
                            HubIdentityProbeResult::Known(daemon_identity),
                        ) if existing_identity == daemon_identity => {
                            Ok(Some(RecordedHubResolution {
                                database_url: daemon,
                                identity_status: RecordedHubIdentityStatus::VerifiedSameHub,
                            }))
                        }
                        (
                            HubIdentityProbeResult::Known(existing_identity),
                            HubIdentityProbeResult::Known(daemon_identity),
                        ) => Err(CoreError::HubConflict {
                            existing_database_url: existing_redacted,
                            existing_identity: existing_identity.display_label(),
                            daemon_database_url: daemon_redacted,
                            daemon_identity: daemon_identity.display_label(),
                        }
                        .into()),
                        (
                            HubIdentityProbeResult::UnknownInsufficientPrivilege { message },
                            _,
                        )
                        | (
                            _,
                            HubIdentityProbeResult::UnknownInsufficientPrivilege { message },
                        ) => Ok(Some(RecordedHubResolution {
                            database_url: existing,
                            identity_status:
                                RecordedHubIdentityStatus::IdentityUnknownInsufficientPrivilege {
                                    message: format!(
                                        "identity_unknown_insufficient_privilege: preserving existing recorded hub {}; daemon hub {} was not adopted because identity could not be verified ({message})",
                                        existing_redacted,
                                        daemon_redacted,
                                    ),
                                },
                        })),
                    }
                }
            }
        }
    }
}

fn redacted_postgres_dsn_placeholder(source: &str) -> String {
    format!("<redacted-{source}-postgres-dsn>")
}

#[cfg(feature = "postgres")]
pub fn probe_postgres_hub_identity(database_url: &str) -> anyhow::Result<HubIdentityProbeResult> {
    use anyhow::Context;
    use postgres::error::SqlState;

    fn insufficient_privilege(error: &postgres::Error) -> bool {
        error.code() == Some(&SqlState::INSUFFICIENT_PRIVILEGE)
    }

    let mut conn = crate::postgres::connect_readonly(database_url)?;
    let has_privilege = match conn.query_one(
        "SELECT has_function_privilege(current_user, 'pg_control_system()', 'execute')",
        &[],
    ) {
        Ok(row) => row.get::<_, bool>(0),
        Err(error) if insufficient_privilege(&error) => {
            return Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                message: "identity_unknown_insufficient_privilege: current role cannot preflight pg_control_system()".to_string(),
            });
        }
        Err(error) => {
            return Err(error).context("failed to preflight pg_control_system() privilege");
        }
    };

    if !has_privilege {
        return Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
            message: "identity_unknown_insufficient_privilege: current role cannot execute pg_control_system()".to_string(),
        });
    }

    let row = match conn.query_one(
        "SELECT system_identifier::text AS system_identifier, current_database() AS database_name FROM pg_control_system()",
        &[],
    ) {
        Ok(row) => row,
        Err(error) if insufficient_privilege(&error) => {
            return Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                message: "identity_unknown_insufficient_privilege: current role cannot execute pg_control_system()".to_string(),
            });
        }
        Err(error) => return Err(error).context("failed to query PostgreSQL hub identity"),
    };

    Ok(HubIdentityProbeResult::Known(HubIdentity {
        system_identifier: row
            .try_get("system_identifier")
            .context("PostgreSQL hub identity did not include system_identifier")?,
        database_name: row
            .try_get("database_name")
            .context("PostgreSQL hub identity did not include database_name")?,
    }))
}

#[cfg(not(feature = "postgres"))]
pub fn probe_postgres_hub_identity(_database_url: &str) -> anyhow::Result<HubIdentityProbeResult> {
    Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
        message: "identity_unknown_insufficient_privilege: gobby-core was built without PostgreSQL support".to_string(),
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HubDatabaseUrlSource {
    Candidate,
    Env,
    GcoreConfig,
    Bootstrap,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct HubDatabaseUrl {
    source: HubDatabaseUrlSource,
    database_url: String,
}

fn resolve_hub_database_urls(
    options: &EnsureHubOptions,
    get_env: &mut impl FnMut(&str) -> Option<String>,
) -> anyhow::Result<Vec<HubDatabaseUrl>> {
    let mut urls = Vec::new();
    urls.extend(
        options
            .candidate_database_urls
            .iter()
            .filter_map(|value| non_empty_trimmed(Some(value.clone())))
            .map(|database_url| HubDatabaseUrl {
                source: HubDatabaseUrlSource::Candidate,
                database_url,
            }),
    );
    if let Some(database_url) = non_empty_trimmed(get_env("GOBBY_POSTGRES_DSN")) {
        urls.push(HubDatabaseUrl {
            source: HubDatabaseUrlSource::Env,
            database_url,
        });
    }
    if let Some(database_url) = resolve_database_url_from_gcore_config(&options.gobby_home)? {
        urls.push(HubDatabaseUrl {
            source: HubDatabaseUrlSource::GcoreConfig,
            database_url,
        });
    }
    if let Some(database_url) =
        resolve_database_url_from_bootstrap_file(&options.gobby_home.join("bootstrap.yaml"))?
    {
        urls.push(HubDatabaseUrl {
            source: HubDatabaseUrlSource::Bootstrap,
            database_url,
        });
    }
    Ok(urls)
}

fn resolve_database_url_from_gcore_config(home: &Path) -> anyhow::Result<Option<String>> {
    if !services_dir(home).is_dir() || !compose_file_path(home).is_file() {
        return Ok(None);
    }
    let Some(config) = StandaloneConfig::read_at(&gcore_config_path(home))? else {
        return Ok(None);
    };
    Ok(config
        .get("databases.postgres.dsn")
        .and_then(|value| non_empty_trimmed(Some(value.to_string()))))
}

#[derive(Debug, Deserialize)]
struct HubBootstrap {
    hub_backend: Option<String>,
    database_url: Option<String>,
}

fn resolve_database_url_from_bootstrap_file(path: &Path) -> anyhow::Result<Option<String>> {
    if !path.exists() {
        return Ok(None);
    }
    let contents = fs::read_to_string(path)
        .with_context(|| format!("failed to read Gobby bootstrap at {}", path.display()))?;
    let bootstrap: HubBootstrap = serde_yaml::from_str(&contents)
        .with_context(|| format!("failed to parse {}", path.display()))?;
    if matches!(bootstrap.hub_backend.as_deref(), Some(backend) if backend != "postgres") {
        return Ok(None);
    }
    Ok(non_empty_trimmed(bootstrap.database_url))
}

fn non_empty_trimmed(value: Option<String>) -> Option<String> {
    let trimmed = value.as_ref()?.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

#[cfg(feature = "postgres")]
fn postgres_database_reachable(database_url: &str) -> bool {
    crate::postgres::connect_readonly(database_url).is_ok()
}

#[cfg(not(feature = "postgres"))]
fn postgres_database_reachable(_database_url: &str) -> bool {
    false
}

#[cfg(feature = "postgres")]
fn explicit_database_url_reachable(
    database_url: &str,
    database_reachable: &mut impl FnMut(&str) -> bool,
) -> bool {
    database_reachable(database_url)
}

#[cfg(not(feature = "postgres"))]
fn explicit_database_url_reachable(
    _database_url: &str,
    _database_reachable: &mut impl FnMut(&str) -> bool,
) -> bool {
    // Without the postgres feature, gcore cannot open a connection to probe an
    // explicit hub DSN. Preserve the configured DSN and let the consumer fail
    // later if it actually needs PostgreSQL access.
    log::warn!(
        "postgres feature is disabled; preserving configured PostgreSQL hub {} without a reachability probe",
        redacted_postgres_dsn_placeholder("explicit")
    );
    true
}
