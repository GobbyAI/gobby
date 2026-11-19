//! DATABASE_URL guard for test code that writes the test hub (design-v2 section 7.1).
//!
//! Verdicts and rule names equal `tests/fixtures/hub_guard.py`; both engines read
//! `tests/fixtures/hub_guard_vectors.json`. The first failing rule refuses:
//! R2 redirect variables, R3 URL identity, R4 rehearsal profile, ARG missing or malformed input.
//! R1 (`GOBBY_TEST_PROTECT`) is retired (ruling H27).

use std::fmt;
use std::fs::{self, File};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};
use tokio_postgres::Config;
use tokio_postgres::config::Host;

/// The isolated test hub's port (ruling H5). Only the test-hub mode checks it.
pub const TEST_HUB_PORT: u16 = 60892;
pub const TEST_HUB_USER: &str = "gobby_test";
pub const TEST_HUB_DBNAME: &str = "gobby_test";
/// Copy of `gobby.cli.hub_backup.rehearsal._SHARED_PORTS`; a test compares it with the vector file.
pub const SHARED_PORTS: &[u16] = &[
    5432, 6333, 6334, 6379, 16379, 60887, 60888, 60889, 60890, 60891, 60892, 60987, 60988, 60990,
    60991, 60992, 60993, 60994, 60995, 60996, 60997, 60998, 60999,
];
pub const HARNESS_DATABASE_PREFIX: &str = "gobby_harness_";
pub const FIXTURE_DATABASE_PREFIX: &str = "gobby_test_";

const REHEARSAL_PROFILE_ENV: &str = "GOBBY_HUB_REHEARSAL_PROFILE";
/// Environment variables that make libpq ignore the URL's host (R2).
const REDIRECT_ENV: &[&str] = &["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"];
/// Query keys that can name a different endpoint or database, even with the expected value (R3).
const FORBIDDEN_QUERY_KEYS: &[&str] = &[
    "dbname",
    "user",
    "host",
    "port",
    "hostaddr",
    "service",
    "servicefile",
    "options",
];
const LOOPBACK_HOSTS: &[&str] = &["127.0.0.1", "localhost", "::1"];
const REHEARSAL_HOSTS: &[&str] = &["127.0.0.1", "::1"];
const MAX_PROFILE_BYTES: u64 = 65536;
const MAX_PID: u32 = 9_999_999;

/// Which identity the accepted base URL names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardMode {
    TestHub,
    Rehearsal,
}

/// An accepted base URL. Per-run URLs derive from `base` only in `TestHub` mode.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HubGuard {
    pub base: String,
    pub mode: GuardMode,
}

/// A refused URL: `rule` is the first failing rule (R2, R3, R4 or ARG).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HubGuardError {
    pub rule: &'static str,
    pub detail: String,
}

impl fmt::Display for HubGuardError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.rule, self.detail)
    }
}

impl std::error::Error for HubGuardError {}

/// Why `database_url_for_tests` returned no URL.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DbUrlError {
    /// `DATABASE_URL` is not set; Rust tests skip with a printed line (ruling H18).
    Unset,
    Refused(HubGuardError),
}

impl fmt::Display for DbUrlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unset => write!(f, "DATABASE_URL is unset"),
            Self::Refused(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for DbUrlError {}

fn refuse(rule: &'static str, detail: impl Into<String>) -> HubGuardError {
    HubGuardError {
        rule,
        detail: detail.into(),
    }
}

/// Accept the base `DATABASE_URL` for a worktree daemon, or refuse it.
///
/// `profile` falls back to `GOBBY_HUB_REHEARSAL_PROFILE` from `env` when `None`.
pub fn check_test_hub(
    url: Option<&str>,
    env: &dyn Fn(&str) -> Option<String>,
    profile: Option<&Path>,
) -> Result<HubGuard, HubGuardError> {
    let url = match url {
        Some(url) if !url.is_empty() => url,
        _ => return Err(refuse("ARG", "DATABASE_URL is unset or empty")),
    };
    check_redirects(env)?;
    let config = parse_url(url, false)?;
    check_endpoint(url, &config)?;
    let profile = match profile {
        Some(path) => Some(path.to_path_buf()),
        None => env(REHEARSAL_PROFILE_ENV)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from),
    };
    match profile {
        Some(path) => check_rehearsal(url, &config, &path),
        None => {
            check_test_hub_identity(&config, TEST_HUB_DBNAME)?;
            Ok(HubGuard {
                base: url.to_owned(),
                mode: GuardMode::TestHub,
            })
        }
    }
}

/// Re-check a per-run URL against the process environment (R2) and the expected database.
pub fn check_derived_url(
    url: &str,
    expected_dbname: &str,
    expected_schema: Option<&str>,
) -> Result<(), HubGuardError> {
    check_derived_url_with_env(url, expected_dbname, expected_schema, &|name| {
        std::env::var(name).ok()
    })
}

/// `check_derived_url` with an explicit environment, for tests that must not see ambient variables.
pub fn check_derived_url_with_env(
    url: &str,
    expected_dbname: &str,
    expected_schema: Option<&str>,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<(), HubGuardError> {
    check_redirects(env)?;
    let config = parse_url(url, true)?;
    check_endpoint(url, &config)?;
    check_test_hub_identity(&config, expected_dbname)?;
    let options = query_keys(url)
        .iter()
        .filter(|key| key.eq_ignore_ascii_case("options"))
        .count();
    if options > 1 {
        return Err(refuse("R3", "the per-run URL repeats options"));
    }
    match (expected_schema, config.get_options()) {
        (None, Some(_)) => Err(refuse(
            "R3",
            "the per-run URL carries options without a schema",
        )),
        (Some(schema), Some(options)) if options != format!("-csearch_path={schema}") => Err(
            refuse("R3", "the per-run URL names an unexpected search_path"),
        ),
        _ => Ok(()),
    }
}

/// Name a per-run database: `gobby_harness_<pid>_<12 lowercase hex>`.
pub fn harness_database_name(pid: u32, run_suffix: &str) -> Result<String, HubGuardError> {
    if !(1..=MAX_PID).contains(&pid) {
        return Err(refuse(
            "ARG",
            "the run pid must be a positive seven-digit number",
        ));
    }
    if !is_run_suffix(run_suffix) {
        return Err(refuse(
            "ARG",
            "the run suffix must be 12 lowercase hex digits",
        ));
    }
    Ok(format!("{HARNESS_DATABASE_PREFIX}{pid}_{run_suffix}"))
}

/// The per-run URL: the accepted base with its database path replaced, or the base in rehearsal.
///
/// `schema` appends exactly `options=-csearch_path%3D<schema>` to the query.
pub fn derive_run_url(
    guard: &HubGuard,
    dbname: &str,
    schema: Option<&str>,
) -> Result<String, HubGuardError> {
    if guard.mode == GuardMode::Rehearsal {
        return Ok(guard.base.clone());
    }
    if run_database_pid(dbname, HARNESS_DATABASE_PREFIX).is_none()
        && run_database_pid(dbname, FIXTURE_DATABASE_PREFIX).is_none()
    {
        return Err(refuse(
            "ARG",
            "a per-run database name must match a harness pattern",
        ));
    }
    let shape = split_url(&guard.base);
    let mut query = shape.query.to_owned();
    if let Some(schema) = schema {
        if schema.is_empty()
            || !schema
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
        {
            return Err(refuse(
                "ARG",
                "the search_path schema name must be a plain identifier",
            ));
        }
        let option = format!("options=-csearch_path%3D{schema}");
        query = if query.is_empty() {
            option
        } else {
            format!("{query}&{option}")
        };
    }
    let mut url = format!("{}/{dbname}", shape.authority);
    if !query.is_empty() {
        url.push('?');
        url.push_str(&query);
    }
    Ok(url)
}

/// The same hub on the `postgres` maintenance database, for CREATE and DROP DATABASE.
pub fn maintenance_url(url: &str) -> String {
    let shape = split_url(url);
    let mut out = format!("{}/postgres", shape.authority);
    if !shape.query.is_empty() {
        out.push('?');
        out.push_str(shape.query);
    }
    out
}

/// Refuse a sweep connection that is not the maintenance database of a loopback gobby_test hub.
pub fn check_maintenance_url(url: &str) -> Result<(), HubGuardError> {
    if split_url(url).path != "/postgres" {
        return Err(refuse(
            "ARG",
            "the sweep must connect to the postgres maintenance database",
        ));
    }
    let config = parse_url(url, false)?;
    check_endpoint(url, &config)?;
    if config.get_user() != Some(TEST_HUB_USER) {
        return Err(refuse(
            "R3",
            "the sweep must connect as the gobby_test user",
        ));
    }
    Ok(())
}

/// The pid in a run database name with `prefix`, when the whole name matches; `None` otherwise.
pub fn run_database_pid(name: &str, prefix: &str) -> Option<u32> {
    let rest = name.strip_prefix(prefix)?;
    let (pid, suffix) = rest.split_once('_')?;
    let valid_pid = !pid.is_empty()
        && pid.len() <= 7
        && !pid.starts_with('0')
        && pid.bytes().all(|byte| byte.is_ascii_digit());
    if !valid_pid || !is_run_suffix(suffix) {
        return None;
    }
    pid.parse().ok()
}

/// The operator's `~/.gobby`, or `None` when no password entry exists for this uid.
///
/// Reads the passwd entry for the effective uid, as the Python guard does (`pwd.getpwuid`), so
/// the environment's `HOME` cannot move the verdict.
pub fn operator_home() -> Option<PathBuf> {
    nix::unistd::User::from_uid(nix::unistd::getuid())
        .ok()
        .flatten()
        .map(|user| user.dir.join(".gobby"))
}

/// `DATABASE_URL` from the process environment, accepted for a test that needs the test hub.
pub fn database_url_for_tests() -> Result<String, DbUrlError> {
    database_url_with_env(&|name| std::env::var(name).ok())
}

/// `database_url_for_tests` with an explicit environment.
pub fn database_url_with_env(env: &dyn Fn(&str) -> Option<String>) -> Result<String, DbUrlError> {
    let url = env("DATABASE_URL").ok_or(DbUrlError::Unset)?;
    if url.is_empty() {
        return Err(DbUrlError::Refused(refuse(
            "ARG",
            "DATABASE_URL must name the isolated test hub",
        )));
    }
    let refused = DbUrlError::Refused;
    check_redirects(env).map_err(refused)?;
    let config = parse_url(&url, false).map_err(refused)?;
    check_endpoint(&url, &config).map_err(refused)?;
    if config.get_user() != Some(TEST_HUB_USER) || config.get_dbname() != Some(TEST_HUB_DBNAME) {
        return Err(refused(refuse(
            "R3",
            "DATABASE_URL must use the gobby_test user and database",
        )));
    }
    Ok(url)
}

fn check_redirects(env: &dyn Fn(&str) -> Option<String>) -> Result<(), HubGuardError> {
    for name in REDIRECT_ENV {
        if env(name).is_some_and(|value| !value.is_empty()) {
            return Err(refuse(
                "R2",
                format!("{name} is set and would redirect libpq off the URL's host"),
            ));
        }
    }
    Ok(())
}

/// Parse with the connecting library and refuse forbidden query keys before any field is read.
fn parse_url(url: &str, allow_options: bool) -> Result<Config, HubGuardError> {
    if !(url.starts_with("postgresql://") || url.starts_with("postgres://")) {
        return Err(refuse(
            "R3",
            "DATABASE_URL must start with postgresql:// or postgres://",
        ));
    }
    for key in query_keys(url) {
        let lowered = key.to_ascii_lowercase();
        let allowed = allow_options && lowered == "options";
        if FORBIDDEN_QUERY_KEYS.contains(&lowered.as_str()) && !allowed {
            return Err(refuse(
                "R3",
                format!("query key {key:?} can redirect the connection"),
            ));
        }
    }
    url.parse::<Config>().map_err(|error| {
        refuse(
            "R3",
            format!("DATABASE_URL does not parse as a connection URL: {error}"),
        )
    })
}

/// Exactly one loopback host and one explicit port; no hostaddr.
fn check_endpoint(url: &str, config: &Config) -> Result<(), HubGuardError> {
    if !config.get_hostaddrs().is_empty() {
        return Err(refuse(
            "R3",
            "hostaddr and service are refused in DATABASE_URL",
        ));
    }
    match config.get_hosts() {
        [Host::Tcp(host)] if LOOPBACK_HOSTS.contains(&host.to_ascii_lowercase().as_str()) => {}
        _ => {
            return Err(refuse(
                "R3",
                "DATABASE_URL must name exactly one loopback host",
            ));
        }
    }
    if config.get_ports().len() != 1 || !explicit_port(url) {
        return Err(refuse("R3", "DATABASE_URL must name exactly one port"));
    }
    Ok(())
}

fn check_test_hub_identity(config: &Config, dbname: &str) -> Result<(), HubGuardError> {
    if config.get_ports().first() != Some(&TEST_HUB_PORT) {
        return Err(refuse(
            "R3",
            format!("DATABASE_URL must use port {TEST_HUB_PORT}"),
        ));
    }
    if config.get_user() != Some(TEST_HUB_USER) {
        return Err(refuse(
            "R3",
            format!("DATABASE_URL must use user {TEST_HUB_USER}"),
        ));
    }
    if config.get_dbname() != Some(dbname) {
        return Err(refuse(
            "R3",
            format!("DATABASE_URL must name database {dbname}"),
        ));
    }
    Ok(())
}

/// Rehearsal mode (R4): the owner-private profile names the same user and database on its own
/// port, and the URL's host and port equal the profile's.
fn check_rehearsal(url: &str, config: &Config, path: &Path) -> Result<HubGuard, HubGuardError> {
    let profile = read_profile(path)?;
    let home = profile
        .get("gobby_home")
        .and_then(Value::as_str)
        .filter(|home| !home.is_empty())
        .ok_or_else(|| refuse("R4", "the rehearsal profile names no gobby_home"))?;
    let operator = operator_home().ok_or_else(|| refuse("R4", "the operator's home is unknown"))?;
    if resolve(Path::new(home)) == resolve(&operator) {
        return Err(refuse(
            "R4",
            "the rehearsal profile's gobby_home is the operator's home",
        ));
    }
    let profile_host = profile
        .get("postgres_host")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let url_host = match config.get_hosts() {
        [Host::Tcp(host)] => host.as_str(),
        _ => "",
    };
    if !REHEARSAL_HOSTS.contains(&profile_host) || url_host != profile_host {
        return Err(refuse(
            "R4",
            "the rehearsal host must equal the URL host exactly",
        ));
    }
    let port = profile
        .get("services")
        .and_then(|services| services.get("postgres"))
        .and_then(|postgres| postgres.get("port"))
        .and_then(Value::as_u64)
        .and_then(|port| u16::try_from(port).ok());
    let port = match port {
        Some(port) if (1024..=65535).contains(&port) && !SHARED_PORTS.contains(&port) => port,
        _ => {
            return Err(refuse(
                "R4",
                "the rehearsal port is missing, shared or privileged",
            ));
        }
    };
    if config.get_ports().first() != Some(&port) {
        return Err(refuse(
            "R4",
            "the URL port must equal the rehearsal profile's port",
        ));
    }
    if config.get_user() != Some(TEST_HUB_USER) || config.get_dbname() != Some(TEST_HUB_DBNAME) {
        return Err(refuse(
            "R4",
            "the rehearsal URL must use gobby_test for user and database",
        ));
    }
    let identified = profile
        .get("postgres_system_identifier")
        .and_then(Value::as_str)
        .is_some_and(|identifier| !identifier.is_empty());
    if !identified {
        return Err(refuse(
            "R4",
            "the rehearsal profile names no postgres_system_identifier",
        ));
    }
    Ok(HubGuard {
        base: url.to_owned(),
        mode: GuardMode::Rehearsal,
    })
}

/// Read the rehearsal profile without following a symlink: absolute, regular, mode 0600, owned by
/// this user, at most `MAX_PROFILE_BYTES`, and a JSON object.
fn read_profile(path: &Path) -> Result<Map<String, Value>, HubGuardError> {
    let unreadable = || {
        refuse(
            "R4",
            "the rehearsal profile is missing, a symlink or unreadable",
        )
    };
    if !path.is_absolute() {
        return Err(refuse("R4", "the rehearsal profile path must be absolute"));
    }
    let metadata = fs::symlink_metadata(path).map_err(|_| unreadable())?;
    if metadata.file_type().is_symlink() {
        return Err(unreadable());
    }
    let mut options = fs::OpenOptions::new();
    options.read(true).custom_flags(nix::libc::O_NOFOLLOW);
    let mut file: File = options.open(path).map_err(|_| unreadable())?;
    let opened = file.metadata().map_err(|_| unreadable())?;
    let owner = current_uid();
    if !opened.is_file() || opened.permissions().mode() & 0o7777 != 0o600 || opened.uid() != owner {
        return Err(refuse(
            "R4",
            "the rehearsal profile must be a regular 0600 file owned by this user",
        ));
    }
    let mut raw = Vec::new();
    file.by_ref()
        .take(MAX_PROFILE_BYTES + 1)
        .read_to_end(&mut raw)
        .map_err(|_| refuse("R4", "the rehearsal profile cannot be read"))?;
    if raw.len() as u64 > MAX_PROFILE_BYTES {
        return Err(refuse("R4", "the rehearsal profile is too large"));
    }
    match serde_json::from_slice::<Value>(&raw) {
        Ok(Value::Object(map)) => Ok(map),
        Ok(_) => Err(refuse("R4", "the rehearsal profile must be a JSON object")),
        Err(_) => Err(refuse("R4", "the rehearsal profile is not JSON")),
    }
}

/// The real uid of this process (`getuid`, as the Python guard's `os.getuid()`).
fn current_uid() -> u32 {
    nix::unistd::getuid().as_raw()
}

/// The path with symlinks and `..` resolved where it exists; the absolute lexical path otherwise.
fn resolve(path: &Path) -> PathBuf {
    fs::canonicalize(path)
        .unwrap_or_else(|_| std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf()))
}

fn is_run_suffix(text: &str) -> bool {
    text.len() == 12
        && text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// The URL split the way `urlsplit` splits it: the fragment goes first, then the query.
struct UrlShape<'a> {
    /// `scheme://userinfo@host:port`
    authority: &'a str,
    path: &'a str,
    query: &'a str,
}

fn split_url(url: &str) -> UrlShape<'_> {
    let without_fragment = url.split('#').next().unwrap_or(url);
    let scheme_end = without_fragment.find("://").map_or(0, |index| index + 3);
    let path_start = without_fragment[scheme_end..]
        .find(['/', '?'])
        .map_or(without_fragment.len(), |index| scheme_end + index);
    let tail = &without_fragment[path_start..];
    let (path, query) = tail.split_once('?').unwrap_or((tail, ""));
    UrlShape {
        authority: &without_fragment[..path_start],
        path,
        query,
    }
}

/// The decoded query keys, read the way `urlsplit(...).query` and `parse_qsl` read them.
fn query_keys(url: &str) -> Vec<String> {
    let query = split_url(url).query;
    query
        .split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| decode_component(pair.split_once('=').map_or(pair, |(key, _)| key)))
        .collect()
}

/// `parse_qsl` decoding: `+` is a space and each valid `%XX` is one byte.
fn decode_component(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => out.push(b' '),
            b'%' => {
                let digits = bytes
                    .get(index + 1..index + 3)
                    .filter(|pair| pair.iter().all(u8::is_ascii_hexdigit));
                if let Some(digits) = digits {
                    let value = std::str::from_utf8(digits)
                        .ok()
                        .and_then(|digits| u8::from_str_radix(digits, 16).ok());
                    if let Some(value) = value {
                        out.push(value);
                        index += 3;
                        continue;
                    }
                }
                out.push(b'%');
            }
            byte => out.push(byte),
        }
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// True when the URL's authority spells a port. Without one, tokio-postgres fills in libpq's 5432,
/// which must not pass as an explicit port.
fn explicit_port(url: &str) -> bool {
    let authority = split_url(url).authority;
    let after_scheme = authority
        .split_once("://")
        .map_or(authority, |(_, rest)| rest);
    let hostport = after_scheme
        .split_once('@')
        .map_or(after_scheme, |(_, hostport)| hostport);
    if let Some(bracketed) = hostport.strip_prefix('[') {
        return bracketed
            .split_once(']')
            .is_some_and(|(_, after)| after.starts_with(':'));
    }
    hostport.contains(':')
}
