use super::*;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DockerServiceOptions {
    pub gobby_home: PathBuf,
    pub postgres_port: u16,
    pub qdrant_http_port: u16,
    pub qdrant_grpc_port: u16,
    pub falkordb_host: String,
    pub falkordb_port: u16,
    pub falkordb_browser_port: u16,
    pub falkordb_password: String,
}

impl DockerServiceOptions {
    pub fn new(gobby_home: PathBuf) -> Self {
        Self {
            gobby_home,
            postgres_port: DEFAULT_POSTGRES_PORT,
            qdrant_http_port: DEFAULT_QDRANT_HTTP_PORT,
            qdrant_grpc_port: DEFAULT_QDRANT_GRPC_PORT,
            falkordb_host: DEFAULT_FALKORDB_HOST.to_string(),
            falkordb_port: DEFAULT_FALKORDB_PORT,
            falkordb_browser_port: DEFAULT_FALKORDB_BROWSER_PORT,
            falkordb_password: DEFAULT_FALKORDB_PASSWORD.to_string(),
        }
    }

    pub fn database_url(&self) -> String {
        default_database_url(self.postgres_port)
    }

    pub fn qdrant_url(&self) -> String {
        format!("http://localhost:{}", self.qdrant_http_port)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceAssetReport {
    pub services_dir: PathBuf,
    pub compose_file: PathBuf,
    pub env_file: PathBuf,
    pub postgres_asset_dir: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DockerProvisioningReport {
    pub services_dir: PathBuf,
    pub compose_file: PathBuf,
    pub env_file: PathBuf,
    pub started_profiles: Vec<String>,
    pub health_checks: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandSpec {
    pub program: String,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub cwd: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub status: i32,
    pub stdout: String,
    pub stderr: String,
}

pub trait CommandRunner {
    fn run(&mut self, spec: &CommandSpec) -> std::io::Result<CommandOutput>;
}

pub struct RealCommandRunner;

impl CommandRunner for RealCommandRunner {
    fn run(&mut self, spec: &CommandSpec) -> std::io::Result<CommandOutput> {
        let mut command = Command::new(&spec.program);
        command.args(&spec.args);
        if let Some(cwd) = &spec.cwd {
            command.current_dir(cwd);
        }
        for (key, value) in &spec.env {
            command.env(key, value);
        }
        let output = command.output()?;
        Ok(CommandOutput {
            status: output.status.code().unwrap_or(1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        })
    }
}

pub trait DockerHealthChecker {
    fn wait_postgres(&mut self, host: &str, port: u16) -> anyhow::Result<()>;
    fn wait_qdrant(&mut self, host: &str, port: u16) -> anyhow::Result<()>;
    fn wait_falkordb(&mut self, host: &str, port: u16) -> anyhow::Result<()>;
}

pub struct TcpDockerHealthChecker {
    pub retries: usize,
    pub interval: Duration,
}

impl Default for TcpDockerHealthChecker {
    fn default() -> Self {
        Self {
            retries: 30,
            interval: Duration::from_secs(2),
        }
    }
}

impl DockerHealthChecker for TcpDockerHealthChecker {
    fn wait_postgres(&mut self, host: &str, port: u16) -> anyhow::Result<()> {
        wait_for_tcp(host, port, self.retries, self.interval)
            .map_err(|err| anyhow::anyhow!("PostgreSQL did not become reachable: {err}"))
    }

    fn wait_qdrant(&mut self, host: &str, port: u16) -> anyhow::Result<()> {
        let healthz = || -> anyhow::Result<()> {
            let mut stream = TcpStream::connect((host, port))?;
            stream.set_read_timeout(Some(Duration::from_secs(3)))?;
            stream.set_write_timeout(Some(Duration::from_secs(3)))?;
            stream.write_all(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")?;
            let mut body = String::new();
            stream.read_to_string(&mut body)?;
            if body.starts_with("HTTP/1.1 200") || body.starts_with("HTTP/1.0 200") {
                Ok(())
            } else {
                anyhow::bail!("unexpected Qdrant health response")
            }
        };
        wait_for(healthz, self.retries, self.interval)
            .map_err(|err| anyhow::anyhow!("Qdrant did not become healthy: {err}"))
    }

    fn wait_falkordb(&mut self, host: &str, port: u16) -> anyhow::Result<()> {
        wait_for_tcp(host, port, self.retries, self.interval)
            .map_err(|err| anyhow::anyhow!("FalkorDB did not become reachable: {err}"))
    }
}

pub fn provision_docker_services(
    options: &DockerServiceOptions,
) -> anyhow::Result<DockerProvisioningReport> {
    let mut runner = RealCommandRunner;
    let mut health = TcpDockerHealthChecker::default();
    provision_docker_services_with(options, &mut runner, &mut health)
}

pub fn provision_docker_services_with(
    options: &DockerServiceOptions,
    runner: &mut impl CommandRunner,
    health: &mut impl DockerHealthChecker,
) -> anyhow::Result<DockerProvisioningReport> {
    let assets = prepare_service_assets(options)?;
    let spec = docker_compose_up_spec(options, &assets.compose_file, &assets.services_dir);
    let output = runner.run(&spec).map_err(|err| {
        anyhow::anyhow!("failed to execute docker compose for standalone services: {err}")
    })?;
    if output.status != 0 {
        anyhow::bail!(
            "docker compose up failed: {}",
            first_non_empty(&output.stderr, &output.stdout)
        );
    }

    health.wait_postgres(DEFAULT_POSTGRES_HOST, options.postgres_port)?;
    health.wait_qdrant(DEFAULT_POSTGRES_HOST, options.qdrant_http_port)?;
    health.wait_falkordb(&options.falkordb_host, options.falkordb_port)?;

    Ok(DockerProvisioningReport {
        services_dir: assets.services_dir,
        compose_file: assets.compose_file,
        env_file: assets.env_file,
        started_profiles: vec!["all".to_string()],
        health_checks: vec![
            "postgres".to_string(),
            "qdrant".to_string(),
            "falkordb".to_string(),
        ],
    })
}

pub fn prepare_service_assets(
    options: &DockerServiceOptions,
) -> anyhow::Result<ServiceAssetReport> {
    let services = services_dir(&options.gobby_home);
    let compose = services.join(COMPOSE_FILENAME);
    let pgsearch = services.join("postgres-pgsearch");
    let env_file = services.join(".env");

    fs::create_dir_all(pgsearch.join("initdb.d"))?;
    fs::create_dir_all(pgsearch.join("scripts"))?;
    fs::write(&compose, COMPOSE_TEMPLATE)?;
    fs::write(pgsearch.join("Dockerfile"), PGSEARCH_DOCKERFILE)?;
    fs::write(pgsearch.join("version.json"), PGSEARCH_VERSION)?;
    fs::write(
        pgsearch.join("initdb.d").join("01-pg_search.sql"),
        PGSEARCH_INIT_PG_SEARCH,
    )?;
    fs::write(
        pgsearch.join("initdb.d").join("02-pgaudit.sql"),
        PGSEARCH_INIT_PGAUDIT,
    )?;
    let audit_script = pgsearch.join("scripts").join("pg_audit_export.sh");
    fs::write(&audit_script, PG_AUDIT_EXPORT)?;
    make_executable(&audit_script)?;

    let manifest = pgsearch_manifest()?;
    update_env_file(
        &env_file,
        BTreeMap::from([
            (
                "GOBBY_PG_SEARCH_VERSION".to_string(),
                manifest.pg_search_version,
            ),
            ("GOBBY_PG_SEARCH_SHA256".to_string(), manifest.sha256),
            (
                "GOBBY_POSTGRES_PORT".to_string(),
                options.postgres_port.to_string(),
            ),
            (
                "GOBBY_POSTGRES_DB".to_string(),
                DEFAULT_POSTGRES_DB.to_string(),
            ),
            (
                "GOBBY_POSTGRES_USER".to_string(),
                DEFAULT_POSTGRES_USER.to_string(),
            ),
            (
                "GOBBY_POSTGRES_PASSWORD".to_string(),
                DEFAULT_POSTGRES_PASSWORD.to_string(),
            ),
            (
                "GOBBY_QDRANT_HTTP_PORT".to_string(),
                options.qdrant_http_port.to_string(),
            ),
            (
                "GOBBY_QDRANT_GRPC_PORT".to_string(),
                options.qdrant_grpc_port.to_string(),
            ),
            (
                "GOBBY_FALKORDB_PORT".to_string(),
                options.falkordb_port.to_string(),
            ),
            (
                "GOBBY_FALKORDB_BROWSER_PORT".to_string(),
                options.falkordb_browser_port.to_string(),
            ),
            (
                "GOBBY_FALKORDB_PASSWORD".to_string(),
                options.falkordb_password.clone(),
            ),
        ]),
    )?;

    Ok(ServiceAssetReport {
        services_dir: services,
        compose_file: compose,
        env_file,
        postgres_asset_dir: pgsearch,
    })
}

pub fn docker_compose_up_spec(
    options: &DockerServiceOptions,
    compose_file: &Path,
    services_dir: &Path,
) -> CommandSpec {
    CommandSpec {
        program: "docker".to_string(),
        args: vec![
            "compose".to_string(),
            "-f".to_string(),
            compose_file.display().to_string(),
            "--profile".to_string(),
            "all".to_string(),
            "up".to_string(),
            "-d".to_string(),
            "--remove-orphans".to_string(),
        ],
        env: BTreeMap::from([
            (
                "GOBBY_FALKORDB_PASSWORD".to_string(),
                options.falkordb_password.clone(),
            ),
            (
                "GOBBY_POSTGRES_PORT".to_string(),
                options.postgres_port.to_string(),
            ),
            (
                "GOBBY_QDRANT_HTTP_PORT".to_string(),
                options.qdrant_http_port.to_string(),
            ),
        ]),
        cwd: Some(services_dir.to_path_buf()),
    }
}

#[derive(Debug, Deserialize)]
struct PgSearchVersionFile {
    pg_search_version: String,
    pg_search_sha256: String,
    pg_search_sha256_by_arch: Option<BTreeMap<String, String>>,
}

struct PgSearchManifest {
    pg_search_version: String,
    sha256: String,
}

fn pgsearch_manifest() -> anyhow::Result<PgSearchManifest> {
    let parsed: PgSearchVersionFile = serde_json::from_str(PGSEARCH_VERSION)?;
    let arch = debian_arch(std::env::consts::ARCH);
    let sha256 = parsed
        .pg_search_sha256_by_arch
        .and_then(|by_arch| by_arch.get(&arch).cloned())
        .unwrap_or(parsed.pg_search_sha256);
    Ok(PgSearchManifest {
        pg_search_version: parsed.pg_search_version,
        sha256,
    })
}

fn debian_arch(arch: &str) -> String {
    match arch {
        "x86_64" | "amd64" => "amd64".to_string(),
        "aarch64" | "arm64" => "arm64".to_string(),
        other => other.to_string(),
    }
}

fn update_env_file(path: &Path, updates: BTreeMap<String, String>) -> anyhow::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut lines = Vec::new();
    if path.exists() {
        for line in fs::read_to_string(path)?.lines() {
            let key = line.split_once('=').map(|(key, _)| key).unwrap_or(line);
            if !updates.contains_key(key) {
                lines.push(line.to_string());
            }
        }
        if lines.last().is_some_and(|line| !line.trim().is_empty()) {
            lines.push(String::new());
        }
    }
    for (key, value) in updates {
        lines.push(format!("{key}={value}"));
    }
    fs::write(path, format!("{}\n", lines.join("\n")))?;
    Ok(())
}

fn first_non_empty<'a>(first: &'a str, second: &'a str) -> &'a str {
    if first.trim().is_empty() {
        second.trim()
    } else {
        first.trim()
    }
}

fn wait_for_tcp(host: &str, port: u16, retries: usize, interval: Duration) -> anyhow::Result<()> {
    wait_for(
        || {
            TcpStream::connect((host, port))
                .map(|_| ())
                .map_err(Into::into)
        },
        retries,
        interval,
    )
}

fn wait_for(
    mut check: impl FnMut() -> anyhow::Result<()>,
    retries: usize,
    interval: Duration,
) -> anyhow::Result<()> {
    let mut last_error = None;
    for attempt in 0..retries {
        match check() {
            Ok(()) => return Ok(()),
            Err(err) => last_error = Some(err),
        }
        if attempt + 1 < retries {
            std::thread::sleep(interval);
        }
    }
    Err(last_error.unwrap_or_else(|| anyhow::anyhow!("health check failed")))
}

fn make_executable(path: &Path) -> anyhow::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut permissions = fs::metadata(path)?.permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(path, permissions)?;
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
    Ok(())
}
