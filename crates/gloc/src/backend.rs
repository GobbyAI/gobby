use crate::config::{Backend, Settings};
use std::fmt;
use std::time::Duration;

#[derive(Debug)]
pub enum ModelError {
    NotFound(String),
    PullFailed(String),
    WarmupFailed(String),
    NetworkError(String),
}

impl fmt::Display for ModelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotFound(m) => write!(f, "model '{}' not found", m),
            Self::PullFailed(msg) => write!(f, "pull failed: {}", msg),
            Self::WarmupFailed(msg) => write!(f, "warmup failed: {}", msg),
            Self::NetworkError(msg) => write!(f, "network error: {}", msg),
        }
    }
}

/// Probe backends in order, return the first that responds with 2xx.
pub fn detect_backend(backends: &[Backend], timeout_ms: u64) -> Option<Backend> {
    let timeout = Duration::from_millis(timeout_ms);
    for backend in backends {
        let url = format!("{}{}", backend.url, backend.probe);
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(timeout)
            .timeout_read(timeout)
            .build();
        if agent.get(&url).call().is_ok() {
            return Some(backend.clone());
        }
    }
    None
}

/// Validate that a specific backend is reachable.
pub fn validate_backend(backend: &Backend, timeout_ms: u64) -> bool {
    let timeout = Duration::from_millis(timeout_ms);
    let url = format!("{}{}", backend.url, backend.probe);
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(timeout)
        .timeout_read(timeout)
        .build();
    agent.get(&url).call().is_ok()
}

/// Ensure a model is ready on the detected backend.
/// LM Studio: no-op (JIT loading).
/// Ollama: check -> pull if needed -> warm up if needed.
pub fn ensure_model_ready(
    backend: &Backend,
    model: &str,
    settings: &Settings,
) -> Result<(), ModelError> {
    if backend.name != "ollama" {
        return Ok(());
    }

    match ollama_check_model(backend, model, settings.probe_timeout_ms) {
        Ok(true) => Ok(()),
        Ok(false) => {
            // Downloaded but not loaded
            if settings.auto_load {
                eprintln!("gloc: loading {} into ollama...", model);
                ollama_warmup_model(backend, model)?;
            }
            Ok(())
        }
        Err(ModelError::NotFound(m)) => {
            if settings.auto_pull {
                eprintln!("gloc: pulling {} from ollama registry...", m);
                ollama_pull_model(backend, &m)?;
                if settings.auto_load {
                    eprintln!("gloc: loading {} into ollama...", m);
                    ollama_warmup_model(backend, &m)?;
                }
                Ok(())
            } else {
                Err(ModelError::NotFound(m))
            }
        }
        Err(e) => Err(e),
    }
}

/// Check if a model is available on Ollama.
/// Returns Ok(true) if loaded in memory, Ok(false) if downloaded but not loaded,
/// Err(NotFound) if not downloaded.
fn ollama_check_model(backend: &Backend, model: &str, timeout_ms: u64) -> Result<bool, ModelError> {
    let timeout = Duration::from_millis(timeout_ms.max(5000));
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(timeout)
        .timeout_read(timeout)
        .build();

    // Check downloaded models
    let tags_url = format!("{}/api/tags", backend.url);
    let resp: serde_json::Value = agent
        .get(&tags_url)
        .call()
        .map_err(|e| ModelError::NetworkError(e.to_string()))?
        .into_json()
        .map_err(|e| ModelError::NetworkError(e.to_string()))?;

    let models = resp
        .get("models")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let downloaded = models.iter().any(|m| model_name_matches(m, model));
    if !downloaded {
        return Err(ModelError::NotFound(model.to_string()));
    }

    // Check if loaded in memory via /api/ps
    let ps_url = format!("{}/api/ps", backend.url);
    if let Ok(ps_resp) = agent.get(&ps_url).call() {
        if let Ok(ps_json) = ps_resp.into_json::<serde_json::Value>() {
            let loaded = ps_json
                .get("models")
                .and_then(|v| v.as_array())
                .map(|arr| arr.iter().any(|m| model_name_matches(m, model)))
                .unwrap_or(false);
            return Ok(loaded);
        }
    }

    Ok(false)
}

/// Pull a model on Ollama (blocking, no streaming).
fn ollama_pull_model(backend: &Backend, model: &str) -> Result<(), ModelError> {
    let url = format!("{}/api/pull", backend.url);
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(10))
        .timeout_read(Duration::from_secs(600))
        .build();

    let body = serde_json::json!({
        "model": model,
        "stream": false
    });

    agent
        .post(&url)
        .send_json(body)
        .map_err(|e| ModelError::PullFailed(e.to_string()))?;

    Ok(())
}

/// Warm up a model on Ollama by triggering a load into memory.
fn ollama_warmup_model(backend: &Backend, model: &str) -> Result<(), ModelError> {
    let url = format!("{}/api/generate", backend.url);
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(10))
        .timeout_read(Duration::from_secs(120))
        .build();

    let body = serde_json::json!({
        "model": model
    });

    agent
        .post(&url)
        .send_json(body)
        .map_err(|e| ModelError::WarmupFailed(e.to_string()))?;

    Ok(())
}

/// Check if an Ollama model entry matches the requested model name.
/// Handles "qwen3-coder" matching "qwen3-coder:latest".
fn model_name_matches(entry: &serde_json::Value, model: &str) -> bool {
    entry
        .get("name")
        .or_else(|| entry.get("model"))
        .and_then(|n| n.as_str())
        .map(|n| {
            n == model || n == format!("{}:latest", model) || n.starts_with(&format!("{}:", model))
        })
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unreachable_backend() -> Backend {
        Backend {
            name: "fake".into(),
            url: "http://127.0.0.1:19999".into(),
            probe: "/".into(),
            auth_token: "".into(),
        }
    }

    #[test]
    fn test_detect_backend_none_running() {
        let backends = vec![unreachable_backend()];
        assert!(detect_backend(&backends, 100).is_none());
    }

    #[test]
    fn test_validate_backend_unreachable() {
        assert!(!validate_backend(&unreachable_backend(), 100));
    }

    #[test]
    fn test_ensure_model_ready_non_ollama_is_noop() {
        let backend = Backend {
            name: "lmstudio".into(),
            url: "http://127.0.0.1:19999".into(),
            probe: "/v1/models".into(),
            auth_token: "lmstudio".into(),
        };
        let settings = Settings::default();
        assert!(ensure_model_ready(&backend, "any-model", &settings).is_ok());
    }

    #[test]
    fn test_model_name_matches_exact() {
        let entry = serde_json::json!({"name": "qwen3-coder"});
        assert!(model_name_matches(&entry, "qwen3-coder"));
    }

    #[test]
    fn test_model_name_matches_with_latest() {
        let entry = serde_json::json!({"name": "qwen3-coder:latest"});
        assert!(model_name_matches(&entry, "qwen3-coder"));
    }

    #[test]
    fn test_model_name_matches_with_tag() {
        let entry = serde_json::json!({"name": "glm-4.7:cloud"});
        assert!(model_name_matches(&entry, "glm-4.7"));
    }

    #[test]
    fn test_model_name_no_match() {
        let entry = serde_json::json!({"name": "llama3"});
        assert!(!model_name_matches(&entry, "qwen3-coder"));
    }

    #[test]
    fn test_model_name_matches_model_field() {
        let entry = serde_json::json!({"model": "qwen3-coder:latest"});
        assert!(model_name_matches(&entry, "qwen3-coder"));
    }

    #[test]
    fn test_model_error_display() {
        assert_eq!(
            ModelError::NotFound("foo".into()).to_string(),
            "model 'foo' not found"
        );
        assert_eq!(
            ModelError::PullFailed("timeout".into()).to_string(),
            "pull failed: timeout"
        );
    }
}
