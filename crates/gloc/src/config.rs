use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::Path;

const DEFAULT_CONFIG: &str = include_str!("../config.yaml");

/// The raw compiled-in config YAML, for `--init` to write to disk.
pub const DEFAULT_CONFIG_YAML: &str = DEFAULT_CONFIG;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub settings: Settings,
    #[serde(default)]
    pub backends: Vec<Backend>,
    #[serde(default)]
    pub clients: BTreeMap<String, Client>,
    #[serde(default)]
    pub aliases: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Settings {
    #[serde(default = "default_probe_timeout_ms")]
    pub probe_timeout_ms: u64,
    #[serde(default = "default_auto_load")]
    pub auto_load: bool,
    #[serde(default)]
    pub auto_pull: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            probe_timeout_ms: default_probe_timeout_ms(),
            auto_load: default_auto_load(),
            auto_pull: false,
        }
    }
}

fn default_probe_timeout_ms() -> u64 {
    500
}

fn default_auto_load() -> bool {
    true
}

#[derive(Debug, Clone, Deserialize)]
pub struct Backend {
    pub name: String,
    pub url: String,
    pub probe: String,
    #[serde(default)]
    pub auth_token: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Client {
    pub binary: String,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default)]
    pub model_flag: String,
    #[serde(default)]
    pub default_model: String,
    #[serde(default)]
    pub default_args: Vec<String>,
    #[serde(default)]
    pub default_env: BTreeMap<String, String>,
}

impl Config {
    /// Load config: CLI override -> .gobby/gloc.yaml -> ~/.gobby/gloc.yaml -> compiled-in default.
    /// First found wins entirely (no merging).
    pub fn load(config_override: Option<&Path>) -> Self {
        if let Some(path) = config_override {
            return Self::load_or_exit(path);
        }

        let project_config = Path::new(".gobby/gloc.yaml");
        if let Some(config) = Self::try_load(project_config) {
            return config;
        }

        if let Some(home) = dirs::home_dir() {
            let global_config = home.join(".gobby/gloc.yaml");
            if let Some(config) = Self::try_load(&global_config) {
                return config;
            }
        }

        serde_yaml::from_str(DEFAULT_CONFIG).expect("built-in config.yaml is invalid")
    }

    fn try_load(path: &Path) -> Option<Self> {
        let content = std::fs::read_to_string(path).ok()?;
        match serde_yaml::from_str(&content) {
            Ok(config) => Some(config),
            Err(e) => {
                eprintln!("Error: failed to parse {}: {e}", path.display());
                eprintln!("Run `gloc --init` to regenerate the default config.");
                std::process::exit(1);
            }
        }
    }

    fn load_or_exit(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(content) => match serde_yaml::from_str(&content) {
                Ok(config) => config,
                Err(e) => {
                    eprintln!("Error: failed to parse {}: {e}", path.display());
                    eprintln!("Run `gloc --init` to regenerate the default config.");
                    std::process::exit(1);
                }
            },
            Err(_) => {
                eprintln!("Error: could not read {}", path.display());
                std::process::exit(1);
            }
        }
    }

    /// Resolve a model alias to the real model name.
    pub fn resolve_alias(&self, model: &str) -> String {
        self.aliases
            .get(model)
            .cloned()
            .unwrap_or_else(|| model.to_string())
    }

    /// Dump the resolved config as YAML-like summary.
    pub fn dump(&self) -> String {
        let mut out = String::new();
        out.push_str("settings:\n");
        out.push_str(&format!(
            "  probe_timeout_ms: {}\n",
            self.settings.probe_timeout_ms
        ));
        out.push_str(&format!("  auto_load: {}\n", self.settings.auto_load));
        out.push_str(&format!("  auto_pull: {}\n", self.settings.auto_pull));

        out.push_str(&format!("\nbackends: {} configured\n", self.backends.len()));
        for b in &self.backends {
            out.push_str(&format!("  {} -> {}{}\n", b.name, b.url, b.probe));
        }

        out.push_str(&format!("\nclients: {} configured\n", self.clients.len()));
        for (name, client) in &self.clients {
            out.push_str(&format!(
                "  {}: binary='{}', default_model='{}'\n",
                name, client.binary, client.default_model
            ));
        }

        if !self.aliases.is_empty() {
            out.push_str(&format!("\naliases: {} defined\n", self.aliases.len()));
            for (alias, target) in &self.aliases {
                out.push_str(&format!("  {} -> {}\n", alias, target));
            }
        }

        out
    }

    /// Load the compiled-in default config directly, bypassing file resolution.
    #[cfg(test)]
    pub fn load_builtin() -> Self {
        serde_yaml::from_str(DEFAULT_CONFIG).expect("built-in config.yaml is invalid")
    }
}

/// Resolve template variables in a string.
/// Supported: {backend.url}, {backend.auth_token}, {backend.name}, {model}
pub fn resolve_template(template: &str, backend: &Backend, model: &str) -> String {
    template
        .replace("{backend.url}", &backend.url)
        .replace("{backend.auth_token}", &backend.auth_token)
        .replace("{backend.name}", &backend.name)
        .replace("{model}", model)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_load_default_config() {
        let config = Config::load_builtin();
        assert_eq!(config.settings.probe_timeout_ms, 500);
        assert!(config.settings.auto_load);
        assert!(!config.settings.auto_pull);
    }

    #[test]
    fn test_default_config_has_backends() {
        let config = Config::load_builtin();
        assert_eq!(config.backends.len(), 2);
        assert_eq!(config.backends[0].name, "lmstudio");
        assert_eq!(config.backends[1].name, "ollama");
    }

    #[test]
    fn test_backend_fields() {
        let config = Config::load_builtin();
        let lms = &config.backends[0];
        assert_eq!(lms.url, "http://localhost:1234");
        assert_eq!(lms.probe, "/v1/models");
        assert_eq!(lms.auth_token, "lmstudio");

        let ollama = &config.backends[1];
        assert_eq!(ollama.url, "http://localhost:11434");
        assert_eq!(ollama.probe, "/api/tags");
        assert_eq!(ollama.auth_token, "ollama");
    }

    #[test]
    fn test_default_config_has_clients() {
        let config = Config::load_builtin();
        assert!(config.clients.contains_key("claude"));
        assert!(config.clients.contains_key("codex"));
    }

    #[test]
    fn test_claude_client_env() {
        let config = Config::load_builtin();
        let claude = config.clients.get("claude").unwrap();
        assert_eq!(claude.binary, "claude");
        assert_eq!(
            claude.env.get("ANTHROPIC_BASE_URL").unwrap(),
            "{backend.url}"
        );
        assert_eq!(
            claude.env.get("ANTHROPIC_AUTH_TOKEN").unwrap(),
            "{backend.auth_token}"
        );
        assert_eq!(claude.env.get("ANTHROPIC_API_KEY").unwrap(), "");
        assert_eq!(claude.model_flag, "--model");
        assert_eq!(claude.default_model, "qwen3-coder");
    }

    #[test]
    fn test_codex_client_env() {
        let config = Config::load_builtin();
        let codex = config.clients.get("codex").unwrap();
        assert_eq!(codex.binary, "codex");
        assert_eq!(
            codex.env.get("OPENAI_BASE_URL").unwrap(),
            "{backend.url}/v1"
        );
        assert_eq!(codex.default_args, vec!["--provider", "openai"]);
    }

    #[test]
    fn test_default_config_has_aliases() {
        let config = Config::load_builtin();
        assert_eq!(config.aliases.get("qwen").unwrap(), "qwen3-coder");
        assert_eq!(config.aliases.get("glm").unwrap(), "glm-4.7:cloud");
    }

    #[test]
    fn test_resolve_alias_hit() {
        let config = Config::load_builtin();
        assert_eq!(config.resolve_alias("qwen"), "qwen3-coder");
    }

    #[test]
    fn test_resolve_alias_miss() {
        let config = Config::load_builtin();
        assert_eq!(config.resolve_alias("unknown-model"), "unknown-model");
    }

    #[test]
    fn test_resolve_template_all_vars() {
        let backend = Backend {
            name: "ollama".into(),
            url: "http://localhost:11434".into(),
            probe: "/api/tags".into(),
            auth_token: "ollama".into(),
        };
        let result = resolve_template("{backend.url}/v1", &backend, "qwen3-coder");
        assert_eq!(result, "http://localhost:11434/v1");
    }

    #[test]
    fn test_resolve_template_auth_token() {
        let backend = Backend {
            name: "lmstudio".into(),
            url: "http://localhost:1234".into(),
            probe: "/v1/models".into(),
            auth_token: "lmstudio".into(),
        };
        let result = resolve_template("{backend.auth_token}", &backend, "m");
        assert_eq!(result, "lmstudio");
    }

    #[test]
    fn test_resolve_template_model() {
        let backend = Backend {
            name: "test".into(),
            url: "http://localhost".into(),
            probe: "/".into(),
            auth_token: "".into(),
        };
        let result = resolve_template("model={model}", &backend, "qwen3-coder");
        assert_eq!(result, "model=qwen3-coder");
    }

    #[test]
    fn test_resolve_template_no_vars() {
        let backend = Backend {
            name: "test".into(),
            url: "http://localhost".into(),
            probe: "/".into(),
            auth_token: "".into(),
        };
        assert_eq!(
            resolve_template("plain-string", &backend, "m"),
            "plain-string"
        );
    }

    #[test]
    fn test_resolve_template_empty() {
        let backend = Backend {
            name: "test".into(),
            url: "http://localhost".into(),
            probe: "/".into(),
            auth_token: "".into(),
        };
        assert_eq!(resolve_template("", &backend, "m"), "");
    }

    #[test]
    fn test_settings_default() {
        let s = Settings::default();
        assert_eq!(s.probe_timeout_ms, 500);
        assert!(s.auto_load);
        assert!(!s.auto_pull);
    }

    #[test]
    fn test_dump_contains_key_sections() {
        let config = Config::load_builtin();
        let dump = config.dump();
        assert!(dump.contains("probe_timeout_ms: 500"));
        assert!(dump.contains("backends: 2 configured"));
        assert!(dump.contains("clients: 2 configured"));
        assert!(dump.contains("aliases: 2 defined"));
    }

    #[test]
    fn test_config_from_valid_override() {
        let path = std::env::temp_dir().join("gloc_test_config.yaml");
        std::fs::write(&path, DEFAULT_CONFIG).unwrap();
        let config = Config::load(Some(&path));
        let _ = std::fs::remove_file(&path);
        assert!(!config.backends.is_empty());
        assert!(!config.clients.is_empty());
    }
}
