mod backend;
mod config;
mod exec;

use clap::Parser;
use config::Config;
use std::path::Path;

#[derive(Parser)]
#[command(
    name = "gloc",
    version,
    about = "Auto-detect local LLM backends and launch AI CLI tools"
)]
struct Cli {
    /// AI client to launch (e.g. claude, codex)
    #[arg(long, value_name = "NAME")]
    client: Option<String>,

    /// LLM backend to use (skips auto-detect)
    #[arg(long, value_name = "NAME")]
    backend: Option<String>,

    /// Model name or alias
    #[arg(long, value_name = "NAME")]
    model: Option<String>,

    /// Override backend URL
    #[arg(long, value_name = "URL")]
    url: Option<String>,

    /// Show detected configuration and exit
    #[arg(long)]
    status: bool,

    /// Write default config to .gobby/gloc.yaml and exit
    #[arg(long)]
    init: bool,

    /// Path to config file (overrides default locations)
    #[arg(long, value_name = "PATH")]
    config: Option<std::path::PathBuf>,

    /// Dump resolved config and exit
    #[arg(long)]
    dump_config: bool,

    /// Arguments passed through to the client binary
    #[arg(last = true)]
    passthrough: Vec<String>,
}

fn main() {
    let cli = Cli::parse();

    // Handle --init early (before config load, since config may not exist yet)
    if cli.init {
        handle_init();
        return;
    }

    let cfg = Config::load(cli.config.as_deref());

    // Auto-export default config to ~/.gobby/gloc.yaml on first run
    if cli.config.is_none() {
        auto_export_config();
    }

    if cli.dump_config {
        print!("{}", cfg.dump());
        return;
    }

    // Resolve backend
    let mut active_backend = resolve_backend(&cli, &cfg);

    // Apply --url override
    if let Some(ref url) = cli.url {
        active_backend.url = url.clone();
    }

    // Resolve client
    let (client_name, client) = resolve_client(&cli, &cfg);

    // Resolve model (--model flag -> client default, then alias resolution)
    let model = resolve_model(&cli, &client, &cfg);

    // --status mode
    if cli.status {
        print_status(&active_backend, &client_name, &client, &model);
        return;
    }

    // Ensure model is ready
    if let Err(e) = backend::ensure_model_ready(&active_backend, &model, &cfg.settings) {
        match e {
            backend::ModelError::NotFound(m) => {
                eprintln!("gloc: model '{}' not found on {}", m, active_backend.name);
                if active_backend.name == "ollama" {
                    eprintln!("  Run: ollama pull {}", m);
                    eprintln!("  Or set auto_pull: true in config");
                }
                std::process::exit(1);
            }
            backend::ModelError::PullFailed(msg) => {
                eprintln!("gloc: failed to pull model: {}", msg);
                std::process::exit(1);
            }
            backend::ModelError::WarmupFailed(msg) => {
                // Non-fatal: model is downloaded, warmup is best-effort
                eprintln!("gloc: warning: model warmup failed: {}", msg);
            }
            backend::ModelError::NetworkError(msg) => {
                eprintln!("gloc: network error: {}", msg);
                std::process::exit(1);
            }
        }
    }

    // Exec into client (does not return)
    exec::exec_client(&client, &active_backend, &model, &cli.passthrough);
}

fn auto_export_config() {
    if let Some(home) = dirs::home_dir() {
        let global_dir = home.join(".gobby");
        let global_config = global_dir.join("gloc.yaml");
        if !global_config.exists() {
            let _ = std::fs::create_dir_all(&global_dir);
            if let Err(e) = std::fs::write(&global_config, config::DEFAULT_CONFIG_YAML) {
                eprintln!("Warning: failed to write {}: {e}", global_config.display());
            }
        }
    }
}

fn handle_init() {
    let project_dir = Path::new(".gobby");
    let project_config = project_dir.join("gloc.yaml");

    if !project_dir.is_dir() {
        eprintln!("Error: .gobby/ directory not found. Run from a Gobby project root.");
        std::process::exit(1);
    }

    if project_config.exists() {
        let bak = project_dir.join("gloc.yaml.bak");
        if let Err(e) = std::fs::rename(&project_config, &bak) {
            eprintln!("Failed to backup .gobby/gloc.yaml: {e}");
            std::process::exit(1);
        }
        eprintln!("Backed up .gobby/gloc.yaml -> .gobby/gloc.yaml.bak");
    }

    if let Err(e) = std::fs::write(&project_config, config::DEFAULT_CONFIG_YAML) {
        eprintln!("Failed to write .gobby/gloc.yaml: {e}");
        std::process::exit(1);
    }
    eprintln!("Created .gobby/gloc.yaml");
}

fn resolve_backend(cli: &Cli, cfg: &Config) -> config::Backend {
    if let Some(ref name) = cli.backend {
        let backend = cfg
            .backends
            .iter()
            .find(|b| b.name == *name)
            .unwrap_or_else(|| {
                eprintln!("gloc: unknown backend '{}'", name);
                eprintln!(
                    "  Available: {}",
                    cfg.backends
                        .iter()
                        .map(|b| b.name.as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                );
                std::process::exit(1);
            })
            .clone();

        if !backend::validate_backend(&backend, cfg.settings.probe_timeout_ms) {
            eprintln!(
                "gloc: backend '{}' at {} is not reachable",
                name, backend.url
            );
            std::process::exit(1);
        }

        backend
    } else {
        match backend::detect_backend(&cfg.backends, cfg.settings.probe_timeout_ms) {
            Some(backend) => {
                eprintln!("gloc: detected {} at {}", backend.name, backend.url);
                backend
            }
            None => {
                eprintln!("gloc: no backend detected");
                eprintln!("  Start LM Studio or Ollama, or use --backend and --url");
                std::process::exit(1);
            }
        }
    }
}

fn resolve_client(cli: &Cli, cfg: &Config) -> (String, config::Client) {
    if let Some(ref name) = cli.client {
        let client = cfg
            .clients
            .get(name)
            .unwrap_or_else(|| {
                eprintln!("gloc: unknown client '{}'", name);
                eprintln!(
                    "  Available: {}",
                    cfg.clients.keys().cloned().collect::<Vec<_>>().join(", ")
                );
                std::process::exit(1);
            })
            .clone();
        (name.clone(), client)
    } else {
        cfg.clients
            .iter()
            .next()
            .map(|(name, client)| (name.clone(), client.clone()))
            .unwrap_or_else(|| {
                eprintln!("gloc: no clients configured");
                std::process::exit(1);
            })
    }
}

fn resolve_model(cli: &Cli, client: &config::Client, cfg: &Config) -> String {
    let raw = cli.model.as_deref().unwrap_or(&client.default_model);
    cfg.resolve_alias(raw)
}

fn print_status(
    backend: &config::Backend,
    client_name: &str,
    client: &config::Client,
    model: &str,
) {
    eprintln!("Backend:  {} ({})", backend.name, backend.url);
    eprintln!("Client:   {} ({})", client_name, client.binary);

    if let Some(path) = exec::which_binary(&client.binary) {
        eprintln!("Binary:   {}", path.display());
    } else {
        eprintln!("Binary:   {} (not found in PATH)", client.binary);
    }

    eprintln!("Model:    {}", model);

    let env = exec::build_env(client, backend, model);
    eprintln!("Env:");
    for (key, val) in &env {
        if val.is_empty() {
            eprintln!("  {}=\"\"", key);
        } else {
            eprintln!("  {}={}", key, val);
        }
    }

    let args = exec::build_args(client, model, &[]);
    if !args.is_empty() {
        eprintln!("Args:     {} {}", client.binary, args.join(" "));
    }
}
