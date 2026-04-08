use serde::de::{self, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use std::collections::BTreeMap;
use std::fmt;
use std::path::Path;

const DEFAULT_CONFIG: &str = include_str!("../config.yaml");

/// The raw compiled-in config YAML, for `--init` to write to disk.
pub const DEFAULT_CONFIG_YAML: &str = DEFAULT_CONFIG;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub settings: Settings,
    #[serde(default)]
    pub pipelines: BTreeMap<String, Pipeline>,
    #[serde(default)]
    pub fallback: Fallback,
    #[serde(default)]
    pub excluded_commands: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Settings {
    #[serde(default = "default_min_output_length")]
    pub min_output_length: usize,
    #[serde(default = "default_max_compressed_lines")]
    pub max_compressed_lines: usize,
    #[serde(default)]
    pub daemon_url: Option<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            min_output_length: default_min_output_length(),
            max_compressed_lines: default_max_compressed_lines(),
            daemon_url: None,
        }
    }
}

fn default_min_output_length() -> usize {
    1000
}

fn default_max_compressed_lines() -> usize {
    100
}

#[derive(Debug, Clone, Deserialize)]
pub struct Pipeline {
    #[serde(rename = "match")]
    pub match_pattern: String,
    #[serde(default)]
    pub steps: Vec<Step>,
}

#[derive(Debug, Clone)]
pub enum Step {
    FilterLines(FilterLinesArgs),
    GroupLines(GroupLinesArgs),
    Truncate(TruncateArgs),
    Dedup(DedupArgs),
}

// Custom deserializer: each step is a YAML map with a single key like
// `filter_lines: {patterns: [...]}` or `dedup: {}`
impl<'de> Deserialize<'de> for Step {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct StepVisitor;

        impl<'de> Visitor<'de> for StepVisitor {
            type Value = Step;

            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str(
                    "a map with a single key: filter_lines, group_lines, truncate, or dedup",
                )
            }

            fn visit_map<A>(self, mut map: A) -> Result<Step, A::Error>
            where
                A: MapAccess<'de>,
            {
                let key: String = map
                    .next_key()?
                    .ok_or_else(|| de::Error::custom("expected a step name"))?;

                let step = match key.as_str() {
                    "filter_lines" => {
                        let args: FilterLinesArgs = map.next_value()?;
                        Step::FilterLines(args)
                    }
                    "group_lines" => {
                        let args: GroupLinesArgs = map.next_value()?;
                        Step::GroupLines(args)
                    }
                    "truncate" => {
                        let args: TruncateArgs = map.next_value()?;
                        Step::Truncate(args)
                    }
                    "dedup" => {
                        let _: serde_yaml::Value = map.next_value()?;
                        Step::Dedup(DedupArgs {})
                    }
                    other => {
                        return Err(de::Error::unknown_variant(
                            other,
                            &["filter_lines", "group_lines", "truncate", "dedup"],
                        ));
                    }
                };

                Ok(step)
            }
        }

        deserializer.deserialize_map(StepVisitor)
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct FilterLinesArgs {
    #[serde(default)]
    pub patterns: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GroupLinesArgs {
    pub mode: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TruncateArgs {
    #[serde(default = "default_head")]
    pub head: usize,
    #[serde(default = "default_tail")]
    pub tail: usize,
    #[serde(default)]
    pub per_file_lines: usize,
    #[serde(default)]
    pub file_marker: String,
}

fn default_head() -> usize {
    20
}

fn default_tail() -> usize {
    10
}

#[derive(Debug, Clone, Deserialize)]
pub struct DedupArgs {}

#[derive(Debug, Clone, Deserialize)]
pub struct Fallback {
    #[serde(default = "default_fallback_steps")]
    pub steps: Vec<Step>,
}

impl Default for Fallback {
    fn default() -> Self {
        Self {
            steps: default_fallback_steps(),
        }
    }
}

fn default_fallback_steps() -> Vec<Step> {
    vec![Step::Truncate(TruncateArgs {
        head: 20,
        tail: 20,
        per_file_lines: 0,
        file_marker: String::new(),
    })]
}

impl Config {
    /// Load config: CLI override → .gobby/gsqz.yaml → ~/.gobby/gsqz.yaml → compiled-in default.
    /// First found wins entirely (no merging).
    pub fn load(config_override: Option<&Path>) -> Self {
        // Priority 1: explicit CLI --config path
        if let Some(path) = config_override {
            return Self::load_or_exit(path);
        }

        // Priority 2: project-level .gobby/gsqz.yaml
        let project_config = Path::new(".gobby/gsqz.yaml");
        if let Some(config) = Self::try_load(project_config) {
            return config;
        }

        // Priority 3: global ~/.gobby/gsqz.yaml
        if let Some(home) = dirs::home_dir() {
            let global_config = home.join(".gobby/gsqz.yaml");
            if let Some(config) = Self::try_load(&global_config) {
                return config;
            }
        }

        // Priority 4: compiled-in default
        serde_yaml::from_str(DEFAULT_CONFIG).expect("built-in config.yaml is invalid")
    }

    /// Try to load config from a path. Returns None if file doesn't exist,
    /// exits with error if file exists but can't be parsed.
    fn try_load(path: &Path) -> Option<Self> {
        let content = std::fs::read_to_string(path).ok()?;
        match serde_yaml::from_str(&content) {
            Ok(config) => Some(config),
            Err(e) => {
                eprintln!("Error: failed to parse {}: {e}", path.display());
                eprintln!("Run `gsqz --init` to regenerate the default config.");
                std::process::exit(1);
            }
        }
    }

    /// Load config from a path or exit with error.
    fn load_or_exit(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(content) => match serde_yaml::from_str(&content) {
                Ok(config) => config,
                Err(e) => {
                    eprintln!("Error: failed to parse {}: {e}", path.display());
                    eprintln!("Run `gsqz --init` to regenerate the default config.");
                    std::process::exit(1);
                }
            },
            Err(_) => {
                eprintln!("Error: could not read {}", path.display());
                std::process::exit(1);
            }
        }
    }

    /// Dump the resolved config as YAML.
    pub fn dump(&self) -> String {
        // Manual dump to avoid serde_yaml output quirks with enums
        let mut out = String::new();
        out.push_str("settings:\n");
        out.push_str(&format!(
            "  min_output_length: {}\n",
            self.settings.min_output_length
        ));
        out.push_str(&format!(
            "  max_compressed_lines: {}\n",
            self.settings.max_compressed_lines
        ));
        if let Some(url) = &self.settings.daemon_url {
            out.push_str(&format!("  daemon_url: \"{}\"\n", url));
        }
        out.push_str(&format!("\npipelines: {} total\n", self.pipelines.len()));
        for (name, pipeline) in &self.pipelines {
            out.push_str(&format!(
                "  {}: match='{}', {} steps\n",
                name,
                pipeline.match_pattern,
                pipeline.steps.len()
            ));
        }
        if !self.excluded_commands.is_empty() {
            out.push_str(&format!(
                "\nexcluded_commands: {:?}\n",
                self.excluded_commands
            ));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_load_default_config() {
        let config = Config::load(None);
        assert_eq!(config.settings.min_output_length, 1000);
        assert_eq!(config.settings.max_compressed_lines, 100);
        assert!(!config.pipelines.is_empty());
    }

    #[test]
    fn test_default_config_has_expected_pipelines() {
        let config = Config::load(None);
        assert!(config.pipelines.contains_key("git-status"));
        assert!(config.pipelines.contains_key("pytest"));
        assert!(config.pipelines.contains_key("cargo-test"));
    }

    #[test]
    fn test_pipeline_has_match_and_steps() {
        let config = Config::load(None);
        let git_status = config.pipelines.get("git-status").unwrap();
        assert!(!git_status.match_pattern.is_empty());
        assert!(!git_status.steps.is_empty());
    }

    #[test]
    fn test_fallback_has_steps() {
        let config = Config::load(None);
        assert!(!config.fallback.steps.is_empty());
    }

    #[test]
    fn test_settings_default_values() {
        let settings = Settings::default();
        assert_eq!(settings.min_output_length, 1000);
        assert_eq!(settings.max_compressed_lines, 100);
        assert!(settings.daemon_url.is_none());
    }

    #[test]
    fn test_step_deserialization_filter() {
        let yaml = "filter_lines:\n  patterns:\n    - '^\\s*$'\n    - '^#'";
        let step: Step = serde_yaml::from_str(yaml).unwrap();
        match step {
            Step::FilterLines(args) => assert_eq!(args.patterns.len(), 2),
            _ => panic!("expected FilterLines"),
        }
    }

    #[test]
    fn test_step_deserialization_group() {
        let yaml = "group_lines:\n  mode: git_status";
        let step: Step = serde_yaml::from_str(yaml).unwrap();
        match step {
            Step::GroupLines(args) => assert_eq!(args.mode, "git_status"),
            _ => panic!("expected GroupLines"),
        }
    }

    #[test]
    fn test_step_deserialization_truncate() {
        let yaml = "truncate:\n  head: 5\n  tail: 3";
        let step: Step = serde_yaml::from_str(yaml).unwrap();
        match step {
            Step::Truncate(args) => {
                assert_eq!(args.head, 5);
                assert_eq!(args.tail, 3);
                assert_eq!(args.per_file_lines, 0);
                assert_eq!(args.file_marker, "");
            }
            _ => panic!("expected Truncate"),
        }
    }

    #[test]
    fn test_step_deserialization_truncate_defaults() {
        let yaml = "truncate: {}";
        let step: Step = serde_yaml::from_str(yaml).unwrap();
        match step {
            Step::Truncate(args) => {
                assert_eq!(args.head, 20);
                assert_eq!(args.tail, 10);
            }
            _ => panic!("expected Truncate"),
        }
    }

    #[test]
    fn test_step_deserialization_dedup() {
        let yaml = "dedup: {}";
        let step: Step = serde_yaml::from_str(yaml).unwrap();
        assert!(matches!(step, Step::Dedup(_)));
    }

    #[test]
    fn test_step_deserialization_unknown_variant() {
        let yaml = "unknown_step: {}";
        let result: Result<Step, _> = serde_yaml::from_str(yaml);
        assert!(result.is_err());
    }

    #[test]
    fn test_config_dump_contains_settings() {
        let config = Config::load(None);
        let dump = config.dump();
        assert!(dump.contains("min_output_length: 1000"));
        assert!(dump.contains("max_compressed_lines: 100"));
        assert!(dump.contains("pipelines:"));
    }

    #[test]
    fn test_fallback_default() {
        let fallback = Fallback::default();
        assert_eq!(fallback.steps.len(), 1);
        match &fallback.steps[0] {
            Step::Truncate(args) => {
                assert_eq!(args.head, 20);
                assert_eq!(args.tail, 20);
            }
            _ => panic!("expected Truncate as default fallback"),
        }
    }

    #[test]
    fn test_config_from_valid_override() {
        // A valid config file should be loaded successfully
        let path = std::env::temp_dir().join("gsqz_test_config.yaml");
        std::fs::write(&path, DEFAULT_CONFIG).unwrap();
        let config = Config::load(Some(&path));
        let _ = std::fs::remove_file(&path);
        assert!(!config.pipelines.is_empty());
    }

    #[test]
    fn test_all_pipeline_regexes_compile() {
        let config = Config::load(None);
        for (name, pipeline) in &config.pipelines {
            assert!(
                regex::Regex::new(&pipeline.match_pattern).is_ok(),
                "pipeline '{}' has invalid regex: {}",
                name,
                pipeline.match_pattern
            );
        }
    }
}
