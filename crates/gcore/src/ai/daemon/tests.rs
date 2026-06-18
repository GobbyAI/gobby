use super::*;
use crate::ai_context::{AiBindings, AiContext, AiLimiter};
use crate::config::{AiRouting, AiTuning, CapabilityBinding, TEST_ENV_LOCK};
use crate::test_http::{RequestHandle, spawn_json_response};
use std::ffi::OsString;
use std::fs;
use std::path::Path;
use std::sync::MutexGuard;

mod embeddings;
mod environment;
mod multipart;
mod text;

fn spawn_server(response: &'static str) -> (u16, RequestHandle) {
    let (api_base, handle) = spawn_json_response(response).expect("spawn test server");
    let port = api_base
        .rsplit(':')
        .next()
        .expect("server port")
        .parse()
        .expect("numeric server port");
    (port, handle)
}

fn request_body_json(request: &str) -> serde_json::Value {
    let (_, body) = request.split_once("\r\n\r\n").unwrap();
    serde_json::from_str(body).unwrap()
}

fn has_header(request: &str, name: &str, value: &str) -> bool {
    request.lines().any(|line| {
        let Some((header_name, header_value)) = line.split_once(':') else {
            return false;
        };
        header_name.eq_ignore_ascii_case(name) && header_value.trim() == value
    })
}

fn multipart_has_field(request: &str, name: &str, value: &str) -> bool {
    request.contains(&format!("name=\"{name}\"\r\n\r\n{value}"))
}

fn temp_home() -> tempfile::TempDir {
    tempfile::tempdir().unwrap()
}

fn write_daemon_files(home: &Path, port: u16, token: &str) {
    let gobby_home = home.join(".gobby");
    fs::create_dir_all(&gobby_home).unwrap();
    fs::write(
        gobby_home.join("bootstrap.yaml"),
        format!("daemon_port: {port}\nbind_host: 127.0.0.1\n"),
    )
    .unwrap();
    fs::write(gobby_home.join("local_cli_token"), format!("{token}\n")).unwrap();
}

fn test_context(project_id: Option<&str>) -> AiContext {
    let binding = binding();
    AiContext {
        bindings: AiBindings {
            embed: binding.clone(),
            audio_transcribe: binding.clone(),
            audio_translate: binding.clone(),
            vision_extract: binding.clone(),
            text_generate: binding,
        },
        tuning: AiTuning {
            max_concurrency: 1,
            keep_alive: None,
        },
        limiter: AiLimiter::new(1),
        project_id: project_id.map(str::to_string),
    }
}

fn binding() -> CapabilityBinding {
    CapabilityBinding {
        routing: AiRouting::Daemon,
        transport: None,
        api_base: None,
        api_key: None,
        model: Some("daemon-model".to_string()),
        provider: Some("daemon-provider".to_string()),
        task: None,
        language: None,
        target_lang: None,
        profile: None,
        candidates: None,
        reasoning_effort: None,
        verify_profile: None,
        verify_model: None,
        verify_api_key: None,
    }
}

struct EnvGuard {
    _lock: MutexGuard<'static, ()>,
    home: Option<OsString>,
    gobby_home: Option<OsString>,
    daemon_url: Option<OsString>,
    port: Option<OsString>,
}

impl EnvGuard {
    fn set_home(home: &Path) -> Self {
        let guard = Self {
            _lock: TEST_ENV_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()),
            home: std::env::var_os("HOME"),
            gobby_home: std::env::var_os("GOBBY_HOME"),
            daemon_url: std::env::var_os("GOBBY_DAEMON_URL"),
            port: std::env::var_os("GOBBY_PORT"),
        };
        // SAFETY: these tests serialize env mutation through TEST_ENV_LOCK,
        // and EnvGuard restores the original values while still holding
        // that lock. GOBBY_DAEMON_URL/GOBBY_PORT are cleared so ambient
        // overrides cannot leak into bootstrap-derived URL assertions.
        unsafe {
            std::env::set_var("HOME", home);
            std::env::set_var("GOBBY_HOME", home.join(".gobby"));
            std::env::remove_var("GOBBY_DAEMON_URL");
            std::env::remove_var("GOBBY_PORT");
        }
        guard
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        // SAFETY: EnvGuard owns the TEST_ENV_LOCK guard for the lifetime of
        // the temporary env override, so restoration cannot race with
        // another test using this helper.
        unsafe {
            for (name, value) in [
                ("HOME", &self.home),
                ("GOBBY_HOME", &self.gobby_home),
                ("GOBBY_DAEMON_URL", &self.daemon_url),
                ("GOBBY_PORT", &self.port),
            ] {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}
