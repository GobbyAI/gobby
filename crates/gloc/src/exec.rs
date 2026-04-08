use crate::config::{Backend, Client, resolve_template};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Command;

/// Build the full environment map for the child process.
/// `default_env` is applied first (lower priority), then `env` overwrites.
/// All values are template-resolved against the backend and model.
pub fn build_env(client: &Client, backend: &Backend, model: &str) -> BTreeMap<String, String> {
    let mut env = BTreeMap::new();

    for (key, val) in &client.default_env {
        env.insert(key.clone(), resolve_template(val, backend, model));
    }

    for (key, val) in &client.env {
        env.insert(key.clone(), resolve_template(val, backend, model));
    }

    env
}

/// Build the command arguments: model flag + model, default_args, passthrough.
pub fn build_args(client: &Client, model: &str, passthrough: &[String]) -> Vec<String> {
    let mut args = Vec::new();

    if !client.model_flag.is_empty() && !model.is_empty() {
        args.push(client.model_flag.clone());
        args.push(model.to_string());
    }

    args.extend(client.default_args.iter().cloned());
    args.extend(passthrough.iter().cloned());

    args
}

/// Look up a binary name in PATH. Returns the full path if found.
pub fn which_binary(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths)
            .map(|dir| dir.join(name))
            .find(|p| p.is_file())
    })
}

/// Exec into the client binary. Does not return on success.
pub fn exec_client(client: &Client, backend: &Backend, model: &str, passthrough: &[String]) -> ! {
    use std::os::unix::process::CommandExt;

    let env = build_env(client, backend, model);
    let args = build_args(client, model, passthrough);

    let mut cmd = Command::new(&client.binary);
    cmd.args(&args);

    for (key, val) in &env {
        cmd.env(key, val);
    }

    let err = cmd.exec();
    eprintln!("gloc: failed to exec {}: {}", client.binary, err);
    std::process::exit(1);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Backend;

    fn test_backend() -> Backend {
        Backend {
            name: "ollama".into(),
            url: "http://localhost:11434".into(),
            probe: "/api/tags".into(),
            auth_token: "ollama".into(),
        }
    }

    fn test_claude_client() -> Client {
        let mut env = BTreeMap::new();
        env.insert("ANTHROPIC_BASE_URL".into(), "{backend.url}".into());
        env.insert("ANTHROPIC_AUTH_TOKEN".into(), "{backend.auth_token}".into());
        env.insert("ANTHROPIC_API_KEY".into(), "".into());
        Client {
            binary: "claude".into(),
            env,
            model_flag: "--model".into(),
            default_model: "qwen3-coder".into(),
            default_args: vec![],
            default_env: BTreeMap::new(),
        }
    }

    fn test_codex_client() -> Client {
        let mut env = BTreeMap::new();
        env.insert("OPENAI_BASE_URL".into(), "{backend.url}/v1".into());
        env.insert("OPENAI_API_KEY".into(), "{backend.auth_token}".into());
        Client {
            binary: "codex".into(),
            env,
            model_flag: "--model".into(),
            default_model: "qwen3-coder".into(),
            default_args: vec!["--provider".into(), "openai".into()],
            default_env: BTreeMap::new(),
        }
    }

    #[test]
    fn test_build_env_claude() {
        let env = build_env(&test_claude_client(), &test_backend(), "qwen3-coder");
        assert_eq!(
            env.get("ANTHROPIC_BASE_URL").unwrap(),
            "http://localhost:11434"
        );
        assert_eq!(env.get("ANTHROPIC_AUTH_TOKEN").unwrap(), "ollama");
        assert_eq!(env.get("ANTHROPIC_API_KEY").unwrap(), "");
    }

    #[test]
    fn test_build_env_codex() {
        let env = build_env(&test_codex_client(), &test_backend(), "qwen3-coder");
        assert_eq!(
            env.get("OPENAI_BASE_URL").unwrap(),
            "http://localhost:11434/v1"
        );
        assert_eq!(env.get("OPENAI_API_KEY").unwrap(), "ollama");
    }

    #[test]
    fn test_build_args_with_model() {
        let args = build_args(&test_claude_client(), "qwen3-coder", &[]);
        assert_eq!(args, vec!["--model", "qwen3-coder"]);
    }

    #[test]
    fn test_build_args_codex_with_defaults() {
        let args = build_args(&test_codex_client(), "qwen3-coder", &[]);
        assert_eq!(args, vec!["--model", "qwen3-coder", "--provider", "openai"]);
    }

    #[test]
    fn test_build_args_with_passthrough() {
        let passthrough = vec!["--verbose".into(), "extra".into()];
        let args = build_args(&test_claude_client(), "qwen3-coder", &passthrough);
        assert_eq!(args, vec!["--model", "qwen3-coder", "--verbose", "extra"]);
    }

    #[test]
    fn test_build_args_empty_model_flag() {
        let mut client = test_codex_client();
        client.model_flag = String::new();
        let args = build_args(&client, "qwen3-coder", &[]);
        assert_eq!(args, vec!["--provider", "openai"]);
    }

    #[test]
    fn test_default_env_lower_priority() {
        let mut client = test_claude_client();
        client
            .default_env
            .insert("ANTHROPIC_BASE_URL".into(), "should-be-overridden".into());
        client
            .default_env
            .insert("EXTRA_VAR".into(), "extra-value".into());
        let env = build_env(&client, &test_backend(), "m");
        assert_eq!(
            env.get("ANTHROPIC_BASE_URL").unwrap(),
            "http://localhost:11434"
        );
        assert_eq!(env.get("EXTRA_VAR").unwrap(), "extra-value");
    }

    #[test]
    fn test_which_binary_finds_sh() {
        // /bin/sh should exist on any unix system
        assert!(which_binary("sh").is_some());
    }

    #[test]
    fn test_which_binary_not_found() {
        assert!(which_binary("definitely_not_a_real_binary_xyz").is_none());
    }
}
