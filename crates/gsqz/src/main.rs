mod compressor;
mod config;
mod daemon;
mod primitives;

use std::process::Command;

use clap::Parser;
use regex::Regex;

use std::sync::LazyLock;

use compressor::Compressor;
use config::Config;

static ANSI_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\x1b\[[0-9;]*[a-zA-Z]").unwrap());

#[derive(Parser)]
#[command(
    name = "gsqz",
    version,
    about = "Run a command and compress its output for LLM consumption"
)]
struct Cli {
    /// Show compression statistics to stderr
    #[arg(long)]
    stats: bool,

    /// Dump resolved config and exit
    #[arg(long)]
    dump_config: bool,

    /// Write the default config to .gobby/gsqz.yaml and exit
    #[arg(long)]
    init: bool,

    /// Path to config file (overrides default locations)
    #[arg(long, value_name = "PATH")]
    config: Option<std::path::PathBuf>,

    /// Command to run (everything after --)
    #[arg(
        trailing_var_arg = true,
        required_unless_present_any = ["dump_config", "init"]
    )]
    command: Vec<String>,
}

fn main() {
    let cli = Cli::parse();

    // Load config
    let config = Config::load(cli.config.as_deref());

    // Auto-export default config to ~/.gobby/gsqz.yaml on first run (creates ~/.gobby/ if needed)
    if cli.config.is_none() && !cli.init {
        if let Some(home) = dirs::home_dir() {
            let global_dir = home.join(".gobby");
            let global_config = global_dir.join("gsqz.yaml");
            if !global_config.exists() {
                let _ = std::fs::create_dir_all(&global_dir);
                if let Err(e) = std::fs::write(&global_config, config::DEFAULT_CONFIG_YAML) {
                    eprintln!("Warning: failed to write {}: {e}", global_config.display());
                } else {
                    eprintln!("Created ~/.gobby/gsqz.yaml with default config.");
                }
            }
        }
    }

    // --init writes to project-level .gobby/gsqz.yaml
    if cli.init {
        let project_dir = std::path::Path::new(".gobby");
        let project_config = project_dir.join("gsqz.yaml");
        if !project_dir.is_dir() {
            eprintln!("Error: .gobby/ directory not found. Run from a Gobby project root.");
            return;
        }
        if project_config.exists() {
            let bak = project_dir.join("gsqz.yaml.bak");
            if let Err(e) = std::fs::rename(&project_config, &bak) {
                eprintln!("Failed to backup .gobby/gsqz.yaml: {e}");
                return;
            }
            eprintln!("Backed up .gobby/gsqz.yaml → .gobby/gsqz.yaml.bak");
        }
        if let Err(e) = std::fs::write(&project_config, config::DEFAULT_CONFIG_YAML) {
            eprintln!("Failed to write .gobby/gsqz.yaml: {e}");
            return;
        }
        eprintln!("Created .gobby/gsqz.yaml");
        return;
    }

    if cli.dump_config {
        print!("{}", config.dump());
        return;
    }

    // Join command args and run through shell
    let cmd = cli.command.join(" ");

    let output = if cfg!(windows) {
        Command::new("cmd").arg("/C").arg(&cmd).output()
    } else {
        Command::new("sh").arg("-c").arg(&cmd).output()
    };

    let output = match output {
        Ok(o) => o,
        Err(e) => {
            eprintln!("gsqz: failed to execute command: {}", e);
            std::process::exit(1);
        }
    };

    let exit_code = output.status.code().unwrap_or(1);

    let mut raw_output = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr_str = String::from_utf8_lossy(&output.stderr);
    if !stderr_str.is_empty() {
        raw_output.push_str(&stderr_str);
    }

    // Strip ANSI escape codes
    let raw_output = ANSI_RE.replace_all(&raw_output, "").to_string();

    if raw_output.trim().is_empty() {
        if exit_code == 0 {
            println!("No errors.");
        } else {
            println!("Command failed with exit code {} (no output).", exit_code);
        }
        // Always exit 0 — the LLM reads pass/fail from content
        return;
    }

    // Try to get daemon config overrides
    let mut compressor_config = config.clone();
    let daemon_url = daemon::resolve_daemon_url(config.settings.daemon_url.as_deref());
    if let Some(ref url) = daemon_url
        && let Some((min_length, max_lines)) = daemon::fetch_daemon_config(url)
    {
        compressor_config.settings.min_output_length = min_length;
        compressor_config.settings.max_compressed_lines = max_lines;
    }

    let compressor = Compressor::new(&compressor_config);
    let result = compressor.compress(&cmd, &raw_output);

    if cli.stats {
        eprintln!(
            "[gsqz] strategy={} original={} compressed={} savings={:.1}%",
            result.strategy_name,
            result.original_chars,
            result.compressed_chars,
            result.savings_pct()
        );
    }

    // Report savings to daemon (best-effort)
    if result.strategy_name != "passthrough"
        && result.strategy_name != "excluded"
        && let Some(ref url) = daemon_url
    {
        daemon::report_savings(
            url,
            &result.strategy_name,
            result.original_chars,
            result.compressed_chars,
        );
    }

    let output_str = if result.strategy_name != "passthrough" && result.strategy_name != "excluded"
    {
        format!(
            "[Output compressed by gsqz — {}, {:.0}% reduction]\n{}",
            result.strategy_name,
            result.savings_pct(),
            result.compressed
        )
    } else {
        result.compressed
    };

    print!("{}", output_str);
    // Always exit 0 — propagating the subprocess exit code causes Claude Code
    // to frame the entire output as "Error: Exit code 1", hiding the results.
}
