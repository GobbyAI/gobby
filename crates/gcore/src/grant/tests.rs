use std::ffi::OsString;
use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::MutexGuard;

use std::thread;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use super::handshake::{
    AGENT_RUN_HEADER, CALLER_PROJECT_HEADER, CapabilityClaims, MANAGED_EXECUTION_HEADER,
    SESSION_HEADER,
};
use super::*;
use crate::local_token::{AUTHORIZATION_HEADER, LOCAL_CLI_TOKEN_FILENAME};

const TOKEN: &str = "operator-token";
const MACHINE: &str = "machine-test";
const PROJECT: &str = "project-test";
const NOW: i64 = 1_700_000_100;

struct Harness {
    _home: tempfile::TempDir,
    _project: tempfile::TempDir,
    home: PathBuf,
    project_root: PathBuf,
}

impl Harness {
    fn new() -> Self {
        let home = tempfile::tempdir().expect("home");
        let project = tempfile::tempdir().expect("project");
        fs::write(home.path().join("machine_id"), MACHINE).expect("machine");
        fs::write(home.path().join(LOCAL_CLI_TOKEN_FILENAME), TOKEN).expect("token");
        let gobby = project.path().join(".gobby");
        fs::create_dir_all(&gobby).expect("project dir");
        fs::write(
            gobby.join("project.json"),
            format!(r#"{{"id":"{PROJECT}","name":"test"}}"#),
        )
        .expect("project json");
        Self {
            home: home.path().to_path_buf(),
            project_root: project.path().to_path_buf(),
            _home: home,
            _project: project,
        }
    }

    fn request<'a>(&'a self, daemon_url: Option<String>) -> AcquireRequest<'a> {
        AcquireRequest {
            project_root: &self.project_root,
            project_id: None,
            home: Some(&self.home),
            daemon_url,
            now: Some(NOW),
            session_id: Some("cli".into()),
            deadline: Some(Duration::from_secs(2)),
            stale_lock_after: Some(Duration::from_millis(200)),
            managed_bootstrap: None,
            managed_envelope: None,
            expected_execution_id: None,
        }
    }
}

#[test]
fn acquire_with_ignores_process_environment() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    write_cache(&harness, &grant, None);

    let poison_dir = tempfile::tempdir().expect("poison home");
    let poison_grant = fixture_grant(PrincipalKind::AgentRun);
    let poison_path = poison_dir.path().join("poison.json");
    write_grant_file(&poison_path, &poison_grant).expect("poison grant");

    let _env = PoisonedEnv::apply(&poison_path);

    let acquired = acquire_with(&harness.request(Some("http://127.0.0.1:1".into())))
        .expect("isolated acquire_with");
    assert_eq!(acquired.source, GrantSource::Cache);
    assert_eq!(acquired.bundle.principal.kind, PrincipalKind::Interactive);

    let process = AcquireRequest::from_process(&harness.project_root);
    assert_eq!(
        process.managed_bootstrap.as_deref(),
        Some(poison_path.as_path())
    );
    assert_eq!(
        process.expected_execution_id.as_deref(),
        Some("not-this-test")
    );
    assert_eq!(process.daemon_url.as_deref(), Some("http://127.0.0.1:9"));
}

#[test]
fn acquire_with_explicit_project_id_needs_no_project_root() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    write_cache(&harness, &grant, None);

    let rootless = tempfile::tempdir().expect("rootless dir");
    let mut request = harness.request(Some("http://127.0.0.1:1".into()));
    request.project_root = rootless.path();
    request.project_id = Some(PROJECT.into());

    let acquired = acquire_with(&request).expect("acquire by explicit project id");
    assert_eq!(acquired.source, GrantSource::Cache);
    assert_eq!(acquired.bundle.principal.project_id, PROJECT);

    let process = AcquireRequest::from_process_for_project_id(PROJECT);
    assert_eq!(process.project_id.as_deref(), Some(PROJECT));
    assert!(process.project_root.as_os_str().is_empty());
}

struct PoisonedEnv {
    _lock: MutexGuard<'static, ()>,
    saved: [(&'static str, Option<OsString>); 5],
}

impl PoisonedEnv {
    fn apply(poison_path: &Path) -> Self {
        let lock = crate::config::TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let saved = [
            "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
            "GOBBY_AGENT_RUN_ID",
            "GOBBY_MANAGED_EXECUTION_ID",
            crate::local_token::AGENT_API_TOKEN_ENV,
            "GOBBY_DAEMON_URL",
        ]
        .map(|name| (name, std::env::var_os(name)));
        // SAFETY: TEST_ENV_LOCK serializes env mutation; Drop restores every
        // saved value, including during unwinding, while the lock is held.
        unsafe {
            std::env::set_var("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", poison_path);
            std::env::set_var("GOBBY_AGENT_RUN_ID", "not-this-test");
            std::env::set_var("GOBBY_MANAGED_EXECUTION_ID", "not-this-test");
            std::env::set_var(crate::local_token::AGENT_API_TOKEN_ENV, "poison-token");
            std::env::set_var("GOBBY_DAEMON_URL", "http://127.0.0.1:9");
        }
        Self { _lock: lock, saved }
    }
}

impl Drop for PoisonedEnv {
    fn drop(&mut self) {
        // SAFETY: the lock remains held until after restoration completes.
        unsafe {
            for (name, value) in &self.saved {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}

fn golden_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/runtime_grants/golden")
}

fn load_golden(name: &str) -> (Vec<u8>, GrantBundle) {
    let raw = fs::read(golden_dir().join(name)).expect(name);
    let grant = serde_json::from_slice::<GrantBundle>(raw.trim_ascii()).expect("parse golden");
    (raw, grant)
}

fn fixture_grant(kind: PrincipalKind) -> GrantBundle {
    let (_, mut grant) = load_golden("direct_datastores.json");
    grant.principal.kind = kind;
    grant.principal.machine_id = MACHINE.into();
    grant.principal.project_id = PROJECT.into();
    grant.principal.session_id = Some("cli".into());
    if kind.is_managed() {
        grant.principal.execution_id = Some("exec-1".into());
    } else {
        grant.principal.execution_id = None;
    }
    grant.deployment.token = "cafebabedeadbeef".into();
    grant.issued_at = NOW - 100;
    grant.expires_at = NOW + 3_500;
    grant.schema_identity = expected_schema_identity();
    grant.api_contract = EXPECTED_API_CONTRACT;
    grant.config_revision = 7;
    grant.signature = "00".repeat(32);
    grant.with_checksum()
}

fn write_cache(harness: &Harness, grant: &GrantBundle, settings: Option<&CachedSettings>) {
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    super::cache::persist_cache(&path, grant, settings).expect("write cache");
}

fn write_torn_two_file_cache(harness: &Harness, grant: &GrantBundle, settings: &CachedSettings) {
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    write_grant_file(&path, grant).expect("write grant");
    fs::write(
        settings_cache_path(&path),
        serde_json::to_vec(settings).expect("settings json"),
    )
    .expect("tear");
}

fn write_binding_for(harness: &Harness, url: &str, token: &str) {
    write_binding(
        &harness.home,
        &TrustedBinding {
            endpoint: url.trim_end_matches('/').to_string(),
            deployment_token: token.to_string(),
        },
    )
    .expect("binding");
}

fn hmac_hex(key: &[u8], data: &[u8]) -> String {
    let pkey = openssl::pkey::PKey::hmac(key).expect("key");
    let mut signer =
        openssl::sign::Signer::new(openssl::hash::MessageDigest::sha256(), &pkey).expect("signer");
    signer.update(data).expect("update");
    let raw = signer.sign_to_vec().expect("sign");
    raw.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn b64url_decode(value: &str) -> Vec<u8> {
    let mut padded = value.replace('-', "+").replace('_', "/");
    while !padded.len().is_multiple_of(4) {
        padded.push('=');
    }
    openssl::base64::decode_block(&padded).expect("b64")
}

fn envelope_token(exp: i64, project_id: &str) -> String {
    let payload = json!({
        "exp": exp,
        "iat": exp - 60,
        "machine_id": MACHINE,
        "project_id": project_id,
        "session_id": "cli",
        "agent_run_id": "exec-1",
    });
    let encoded = openssl::base64::encode_block(payload.to_string().as_bytes())
        .replace('+', "-")
        .replace('/', "_")
        .trim_end_matches('=')
        .to_string();
    let sig = openssl::base64::encode_block(b"sig-bytes-for-tests-32!!!!!!!!!!!")
        .replace('+', "-")
        .replace('/', "_")
        .trim_end_matches('=')
        .to_string();
    format!("gobby-agent-v1.{encoded}.{sig}")
}

#[derive(Clone)]
enum Step {
    Challenge { valid: bool, token: String },
    Handshake { grant: Box<GrantBundle> },
    Config { revision: i64 },
    Reject { status: u16, body: String },
}

struct Scripted {
    url: String,
    requests: thread::JoinHandle<Vec<String>>,
}

fn read_http(stream: &mut impl Read) -> String {
    let mut request = Vec::new();
    let mut chunk = [0_u8; 1024];
    loop {
        let read = match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(read) => read,
            Err(_) => break,
        };
        request.extend_from_slice(&chunk[..read]);
        if let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") {
            let header = String::from_utf8_lossy(&request[..header_end]);
            let content_length = header.lines().find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.trim()
                    .eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().ok())
                    .flatten()
            });
            if let Some(length) = content_length {
                if request.len().saturating_sub(header_end + 4) >= length {
                    break;
                }
            } else {
                break;
            }
        }
    }
    String::from_utf8_lossy(&request).into_owned()
}

fn spawn_scripted(steps: Vec<Step>) -> Scripted {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let url = format!("http://{}", listener.local_addr().expect("addr"));
    let requests = thread::spawn(move || {
        let mut captured = Vec::new();
        for step in steps {
            let request = loop {
                let Ok((mut stream, _)) = listener.accept() else {
                    return captured;
                };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                let request = read_http(&mut stream);
                if request.contains("HTTP/") {
                    let (status, body) = match &step {
                        Step::Challenge { valid, token } => {
                            let nonce = request
                                .split("\"nonce\":\"")
                                .nth(1)
                                .and_then(|rest| rest.split('"').next())
                                .unwrap_or_default();
                            let proof = if *valid {
                                hmac_hex(token.as_bytes(), &b64url_decode(nonce))
                            } else {
                                "00".repeat(32)
                            };
                            (200, json!({"proof": proof}).to_string())
                        }
                        Step::Handshake { grant } => (
                            200,
                            json!({
                                "grant": serde_json::to_value(grant).expect("grant json"),
                                "deployment_token": grant.deployment.token,
                                "fencing_epoch": grant.deployment.fencing_epoch,
                            })
                            .to_string(),
                        ),
                        Step::Config { revision } => (
                            200,
                            json!({
                                "config_revision": revision,
                                "settings": {"ai.embeddings.model": "nomic"}
                            })
                            .to_string(),
                        ),
                        Step::Reject { status, body } => (*status, body.clone()),
                    };
                    let _ = write!(
                        stream,
                        "HTTP/1.1 {status} OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    break request;
                }
            };
            captured.push(request);
        }
        captured
    });
    Scripted { url, requests }
}

fn join(scripted: Scripted) -> Vec<String> {
    scripted.requests.join().expect("server")
}

fn has_header(request: &str, name: &str) -> bool {
    request.lines().any(|line| {
        line.split_once(':')
            .is_some_and(|(header, _)| header.eq_ignore_ascii_case(name))
    })
}

fn header_value(request: &str, name: &str) -> Option<String> {
    request.lines().find_map(|line| {
        line.split_once(':').and_then(|(header, value)| {
            header
                .eq_ignore_ascii_case(name)
                .then(|| value.trim().to_string())
        })
    })
}

#[test]
fn golden_vectors_match_python() {
    let mut files = 0;
    for entry in fs::read_dir(golden_dir()).expect("golden dir") {
        let entry = entry.expect("entry");
        if entry.path().extension().and_then(|ext| ext.to_str()) != Some("json") {
            continue;
        }
        if entry.file_name() == "payload_skew_unknown_field.json" {
            continue;
        }
        files += 1;
        let raw = fs::read(entry.path()).expect("read");
        let grant: GrantBundle = serde_json::from_slice(raw.trim_ascii())
            .unwrap_or_else(|error| panic!("{}: {error}", entry.path().display()));
        let canonical = grant.model_dump_canonical().expect("canonical");
        assert_eq!(canonical, raw, "{}", entry.path().display());
        verify_payload_checksum(&grant).expect("checksum");
    }
    assert!(files >= 3, "expected the 1.2 golden set");
}

#[test]
fn api_contract_gate() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.api_contract = 99;
    grant = grant.with_checksum();
    let path = harness.home.join("managed.json");
    write_grant_file(&path, &grant).expect("write");
    let mut request = harness.request(None);
    request.managed_bootstrap = Some(path.clone());
    let error = acquire_with(&request).expect_err("contract");
    assert_eq!(
        error,
        GrantError::ApiContractMismatch {
            grant_contract: Some(99),
            binary_contract: EXPECTED_API_CONTRACT,
            source: Some(format!("managed grant file {}", path.display())),
        }
    );
    assert_eq!(error.cli_code(), "api_contract_mismatch");
    assert_eq!(error.exit_status(), 2);

    let old_client = golden_dir().join("old_client_new_grant.json");
    if old_client.exists() {
        let raw = fs::read(old_client).expect("old/new golden");
        let grant: GrantBundle = serde_json::from_slice(raw.trim_ascii()).expect("parse");
        assert_ne!(grant.api_contract, EXPECTED_API_CONTRACT);
        assert_eq!(
            validate_for_construction(
                &grant,
                &grant.principal.project_id,
                &grant.principal.machine_id,
                None,
                None,
                grant.principal.kind.is_managed()
            ),
            Err(GrantError::ApiContractMismatch {
                grant_contract: Some(grant.api_contract),
                binary_contract: EXPECTED_API_CONTRACT,
                source: None,
            })
        );
    } else {
        let (_, mut grant) = load_golden("direct_datastores.json");
        grant.api_contract = EXPECTED_API_CONTRACT + 1;
        grant = grant.with_checksum();
        assert_eq!(
            validate_for_construction(
                &grant,
                &grant.principal.project_id,
                &grant.principal.machine_id,
                None,
                None,
                false
            ),
            Err(GrantError::ApiContractMismatch {
                grant_contract: Some(grant.api_contract),
                binary_contract: EXPECTED_API_CONTRACT,
                source: None,
            })
        );
    }
}

#[test]
fn unknown_grant_field_with_matching_contract_is_payload_skew() {
    let grant = fixture_grant(PrincipalKind::Interactive);
    let mut value = serde_json::to_value(&grant).expect("json");
    value
        .as_object_mut()
        .expect("object")
        .insert("credential_generation".into(), json!(1));
    let raw = serde_json::to_vec(&value).expect("serialize");
    let error = parse_grant_json(&raw).expect_err("skew");
    assert!(
        matches!(error, GrantError::PayloadSkew { .. }),
        "expected PayloadSkew, got {error:?}"
    );
    assert_eq!(error.cli_code(), "payload_skew");
    assert_eq!(error.exit_status(), 2);
    assert!(
        error.to_string().starts_with("grant payload skew:"),
        "typed skew must wrap the serde dump, got {}",
        error
    );
}

#[test]
fn wrong_api_contract_parses_to_enriched_mismatch() {
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.api_contract = 99;
    grant = grant.with_checksum();
    let raw = serde_json::to_vec(&grant).expect("serialize");
    let error = parse_grant_json(&raw).expect_err("mismatch");
    assert_eq!(
        error,
        GrantError::ApiContractMismatch {
            grant_contract: Some(99),
            binary_contract: EXPECTED_API_CONTRACT,
            source: None,
        }
    );
    assert_eq!(
        error.to_string(),
        format!(
            "grant api contract 99 does not match this binary's supported contract {}",
            EXPECTED_API_CONTRACT
        )
    );
    assert_eq!(error.cli_code(), "api_contract_mismatch");
    assert_eq!(error.exit_status(), 2);
}

#[test]
fn absent_api_contract_parses_to_enriched_mismatch() {
    let grant = fixture_grant(PrincipalKind::Interactive);
    let mut value = serde_json::to_value(&grant).expect("json");
    value
        .as_object_mut()
        .expect("object")
        .remove("api_contract");
    let raw = serde_json::to_vec(&value).expect("serialize");
    let error = parse_grant_json(&raw).expect_err("absent");
    assert_eq!(
        error,
        GrantError::ApiContractMismatch {
            grant_contract: None,
            binary_contract: EXPECTED_API_CONTRACT,
            source: None,
        }
    );
    assert_eq!(
        error.to_string(),
        format!(
            "grant api contract unknown does not match this binary's supported contract {}",
            EXPECTED_API_CONTRACT
        )
    );
    assert_eq!(error.cli_code(), "api_contract_mismatch");
    assert_eq!(error.exit_status(), 2);
}

#[test]
fn schema_mismatch_refuses_construction() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.schema_identity.baseline_version += 1;
    grant = grant.with_checksum();
    let path = harness.home.join("managed.json");
    write_grant_file(&path, &grant).expect("write");
    let mut request = harness.request(None);
    request.managed_bootstrap = Some(path.clone());
    let error = acquire_with(&request).expect_err("schema");
    assert_eq!(error, GrantError::SchemaMismatch);
}

#[test]
fn stale_cached_schema_rehandshakes_when_reachable() {
    let harness = Harness::new();
    let mut cached = fixture_grant(PrincipalKind::Interactive);
    cached.deployment.token = deployment_token(&harness.home);
    cached.schema_identity.latest_version -= 1;
    cached = cached.with_checksum();
    write_cache(&harness, &cached, None);
    write_binding_for(&harness, "http://127.0.0.1:1", &cached.deployment.token);

    let mut fresh = fixture_grant(PrincipalKind::Interactive);
    fresh.deployment.token = cached.deployment.token.clone();
    fresh = fresh.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(fresh.clone()),
        },
        Step::Config {
            revision: fresh.config_revision,
        },
    ]);
    write_binding_for(&harness, &scripted.url, &fresh.deployment.token);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("rehandshake");
    let _ = join(scripted);
    assert_eq!(acquired.source, GrantSource::Handshake);
    assert_eq!(acquired.bundle.schema_identity, expected_schema_identity());
}

#[test]
fn stale_cached_schema_stays_mismatch_offline() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant.schema_identity.latest_version -= 1;
    grant = grant.with_checksum();
    write_cache(&harness, &grant, None);
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    let error =
        acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect_err("offline");
    assert_eq!(error, GrantError::SchemaMismatch);
}

#[test]
fn corrupt_grant_refused_offline() {
    // A corrupt interactive cache is never a credential source: it degrades
    // to a cache miss (discarding the file), which offline means the daemon
    // is required for a fresh handshake.
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    write_grant_file(&path, &grant).expect("write");
    let mut raw = fs::read(&path).expect("read");
    raw[0] ^= 0x01;
    fs::write(&path, raw).expect("corrupt payload");
    let error =
        acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect_err("payload");
    assert_eq!(error, GrantError::DaemonRequired);
    assert!(!path.exists(), "undeserializable cache must be discarded");

    write_grant_file(&path, &grant).expect("rewrite");
    let mut parsed: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    parsed["payload_checksum"] = json!("ff".repeat(32));
    fs::write(&path, serde_json::to_vec(&parsed).unwrap()).unwrap();
    let error =
        acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect_err("checksum");
    assert_eq!(error, GrantError::DaemonRequired);
    assert!(
        !path.exists(),
        "checksum-mismatched cache must be discarded"
    );
}

#[test]
fn checksum_mismatched_cache_rehandshakes_and_rewrites_valid_cache() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant.config_revision = 2;
    grant = grant.with_checksum();
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    write_grant_file(&path, &grant).expect("write");
    let mut parsed: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    parsed["payload_checksum"] = json!("ff".repeat(32));
    fs::write(&path, serde_json::to_vec(&parsed).unwrap()).unwrap();

    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config { revision: 2 },
    ]);
    write_binding_for(&harness, &scripted.url, &grant.deployment.token);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("rehandshake");
    let _ = join(scripted);
    assert_eq!(acquired.source, GrantSource::Handshake);
    match super::cache::inspect_cache_pair(&path).expect("rewritten cache must parse") {
        Some(_) => {}
        None => panic!("handshake must rewrite a valid cache"),
    }
}

#[test]
fn undeserializable_cache_rehandshakes_and_rewrites_valid_cache() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant.config_revision = 2;
    grant = grant.with_checksum();
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    fs::create_dir_all(path.parent().expect("parent")).expect("dirs");
    fs::write(&path, b"{ not json ").expect("write");

    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config { revision: 2 },
    ]);
    write_binding_for(&harness, &scripted.url, &grant.deployment.token);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("rehandshake");
    let _ = join(scripted);
    assert_eq!(acquired.source, GrantSource::Handshake);
    match super::cache::inspect_cache_pair(&path).expect("rewritten cache must parse") {
        Some(_) => {}
        None => panic!("handshake must rewrite a valid cache"),
    }
}

#[test]
fn inspect_is_non_authorizing() {
    let harness = Harness::new();
    assert!(matches!(
        inspect_cached_grant_at(
            &harness.project_root,
            Some(&harness.home),
            Some("http://127.0.0.1:60887"),
            Some(NOW)
        ),
        CachedGrantInspection::Absent
    ));
    let grant = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(&harness, "http://127.0.0.1:60887", &grant.deployment.token);
    write_cache(&harness, &grant, None);
    let inspected = inspect_cached_grant_at(
        &harness.project_root,
        Some(&harness.home),
        Some("http://127.0.0.1:60887"),
        Some(NOW),
    );
    assert!(matches!(inspected, CachedGrantInspection::Valid { .. }));
    let debug = format!("{inspected:?}");
    assert!(!debug.contains("postgresql://"));
    assert!(!debug.contains("falkor-secret"));
    assert!(!debug.contains("qdrant-secret"));

    let mut expiring = grant.clone();
    expiring.issued_at = NOW - 100;
    expiring.expires_at = NOW + 10;
    expiring = expiring.with_checksum();
    write_cache(&harness, &expiring, None);
    assert!(matches!(
        inspect_cached_grant_at(
            &harness.project_root,
            Some(&harness.home),
            Some("http://127.0.0.1:60887"),
            Some(NOW)
        ),
        CachedGrantInspection::Expiring { .. }
    ));

    let mut expired = grant;
    expired.expires_at = NOW - 1;
    expired = expired.with_checksum();
    write_cache(&harness, &expired, None);
    assert!(matches!(
        inspect_cached_grant_at(
            &harness.project_root,
            Some(&harness.home),
            Some("http://127.0.0.1:60887"),
            Some(NOW)
        ),
        CachedGrantInspection::Expired { .. }
    ));
}

#[test]
fn outage_window_semantics() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    write_cache(&harness, &grant, None);
    let acquired =
        acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect("cached");
    assert!(acquired.permits_datastore());
    assert!(!acquired.permits_ai());

    let mut expired = grant;
    expired.expires_at = NOW - 1;
    expired = expired.with_checksum();
    write_cache(&harness, &expired, None);
    let error =
        acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect_err("expired");
    assert_eq!(error, GrantError::DaemonRequired);
}

#[test]
fn managed_grant_never_overwrites_interactive_cache() {
    let harness = Harness::new();
    let interactive = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(
        &harness,
        "http://127.0.0.1:1",
        &interactive.deployment.token,
    );
    write_cache(&harness, &interactive, None);
    let cache_path =
        interactive_cache_path(&harness.home, &interactive.deployment.token, PROJECT, None);
    let before = fs::read(&cache_path).expect("before");

    let mut managed = fixture_grant(PrincipalKind::AgentRun);
    managed.deployment.token = interactive.deployment.token.clone();
    managed = managed.with_checksum();
    let managed_path = harness.home.join("agent-grant.json");
    write_grant_file(&managed_path, &managed).expect("managed");
    let mut request = harness.request(Some("http://127.0.0.1:1".into()));
    request.managed_bootstrap = Some(managed_path.clone());
    let acquired = acquire_with(&request).expect("managed");
    assert_eq!(acquired.source, GrantSource::ManagedFile);
    assert_eq!(acquired.bundle.principal.kind, PrincipalKind::AgentRun);
    assert_eq!(fs::read(&cache_path).expect("after"), before);
}

#[test]
fn remote_endpoint_refused_before_auth() {
    let harness = Harness::new();
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let error = acquire_with(&harness.request(Some(format!("http://example.com:{port}"))))
        .expect_err("remote");
    assert_eq!(error, GrantError::RemoteEndpoint);
}

#[test]
fn substituted_listener_gets_no_bearer() {
    let harness = Harness::new();
    let scripted = spawn_scripted(vec![Step::Challenge {
        valid: false,
        token: TOKEN.into(),
    }]);
    let url = scripted.url.clone();
    let error = acquire_with(&harness.request(Some(url))).expect_err("proof");
    assert!(matches!(error, GrantError::Malformed(_)));
    let requests = join(scripted);
    assert_eq!(requests.len(), 1);
    assert!(requests[0].contains("POST /api/runtime/handshake/challenge"));
    assert!(!has_header(&requests[0], AUTHORIZATION_HEADER));
    assert!(load_binding(&harness.home, &requests[0]).is_none());
}

#[test]
fn acquire_resolves_managed_then_cache_then_handshake() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant = grant.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config {
            revision: grant.config_revision,
        },
    ]);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("handshake");
    assert_eq!(acquired.source, GrantSource::Handshake);
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    let _ = join(scripted);
    let cache = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&cache).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    let cached = acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect("cache");
    assert_eq!(cached.source, GrantSource::Cache);

    let managed = fixture_grant(PrincipalKind::ToolChat);
    let managed_path = harness.home.join("tool.json");
    write_grant_file(&managed_path, &managed).expect("managed");
    let mut request = harness.request(Some("http://127.0.0.1:1".into()));
    request.managed_bootstrap = Some(managed_path.clone());
    let acquired = acquire_with(&request).expect("managed");
    assert_eq!(acquired.source, GrantSource::ManagedFile);
}

fn mark_as_worktree(harness: &Harness, parent_root: &Path) -> String {
    fs::write(
        harness.project_root.join(".gobby").join("project.json"),
        format!(r#"{{"id":"{PROJECT}","name":"test"}}"#),
    )
    .expect("worktree project json");
    fs::write(
        harness.project_root.join(".gobby").join("isolation.json"),
        format!(
            r#"{{"parent_project_path":"{}","parent_project_id":"{PROJECT}"}}"#,
            parent_root.display()
        ),
    )
    .expect("worktree isolation json");
    crate::project::code_index_id_for_root(&harness.project_root)
}

#[test]
fn worktree_handshake_sends_overlay_and_caches_under_it() {
    let harness = Harness::new();
    let parent = tempfile::tempdir().expect("parent");
    let overlay = mark_as_worktree(&harness, parent.path());
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant.principal.code_overlay_project_id = Some(overlay.clone());
    grant = grant.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config {
            revision: grant.config_revision,
        },
    ]);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("handshake");
    assert_eq!(acquired.source, GrantSource::Handshake);
    assert_eq!(
        acquired.bundle.principal.code_overlay_project_id.as_deref(),
        Some(overlay.as_str())
    );
    let requests = join(scripted);
    let handshake = requests
        .iter()
        .find(|request| request.contains("POST /api/runtime/handshake HTTP"))
        .expect("handshake request");
    assert!(
        handshake.contains(&format!("\"code_overlay_project_id\":\"{overlay}\"")),
        "handshake body must name the overlay: {handshake}"
    );
    // Interactive handshakes carry no managed identity headers.
    assert!(!has_header(handshake, CALLER_PROJECT_HEADER));
    assert!(!has_header(handshake, SESSION_HEADER));
    assert!(!has_header(handshake, AGENT_RUN_HEADER));
    assert!(!has_header(handshake, MANAGED_EXECUTION_HEADER));
    let overlay_cache = interactive_cache_path(
        &harness.home,
        &grant.deployment.token,
        PROJECT,
        Some(&overlay),
    );
    assert!(
        overlay_cache.is_file(),
        "overlay grant cached under its own name"
    );
    assert!(
        !interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None).exists(),
        "main checkout cache must stay untouched"
    );
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    let cached = acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect("cache");
    assert_eq!(cached.source, GrantSource::Cache);
}

fn interactive_handshake_body(harness: &Harness, session_id: Option<&str>) -> String {
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant = grant.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config {
            revision: grant.config_revision,
        },
    ]);
    let mut request = harness.request(Some(scripted.url.clone()));
    request.session_id = session_id.map(ToOwned::to_owned);
    acquire_with(&request).expect("handshake");
    let requests = join(scripted);
    requests
        .iter()
        .find(|request| request.contains("POST /api/runtime/handshake HTTP"))
        .expect("handshake request")
        .clone()
}

#[test]
fn interactive_handshake_presents_the_real_session_id() {
    let harness = Harness::new();
    let session = "0a80e7a4-6f6e-4b7a-9d3e-2f1c5b7a9c11";
    let handshake = interactive_handshake_body(&harness, Some(session));
    assert!(
        handshake.contains(&format!("\"session_id\":\"{session}\"")),
        "handshake body must carry the caller's session id: {handshake}"
    );
}

#[test]
fn interactive_handshake_without_session_sends_null_not_a_minted_id() {
    for absent in [None, Some("not-a-uuid")] {
        let handshake = interactive_handshake_body(&Harness::new(), absent);
        assert!(
            handshake.contains("\"session_id\":null"),
            "a missing or invalid session must be presented as null, never fabricated: {handshake}"
        );
    }
}

#[test]
fn main_checkout_handshake_omits_overlay() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant = grant.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config {
            revision: grant.config_revision,
        },
    ]);
    acquire_with(&harness.request(Some(scripted.url.clone()))).expect("handshake");
    let requests = join(scripted);
    let handshake = requests
        .iter()
        .find(|request| request.contains("POST /api/runtime/handshake HTTP"))
        .expect("handshake request");
    assert!(!handshake.contains("code_overlay_project_id"));
}

#[test]
fn cached_grant_with_foreign_overlay_is_rejected() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.principal.code_overlay_project_id = Some("bee23f80-d127-5e8f-9dd1-30670378e19a".into());
    grant = grant.with_checksum();
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    write_cache(&harness, &grant, None);
    let error = acquire_with(&harness.request(Some("http://127.0.0.1:1".into())))
        .expect_err("overlay grant must not serve the main checkout");
    assert!(
        matches!(&error, GrantError::Malformed(reason) if reason.contains("overlay")),
        "{error:?}"
    );
}

#[test]
fn renewal_is_non_blocking_past_half_ttl() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.issued_at = NOW - 100;
    grant.expires_at = NOW + 10;
    grant = grant.with_checksum();
    write_binding_for(&harness, "http://127.0.0.1:9", &grant.deployment.token);
    write_cache(&harness, &grant, None);
    let lock = grant_lock_path(&interactive_cache_path(
        &harness.home,
        &grant.deployment.token,
        PROJECT,
        None,
    ));
    let _held = try_lock(&lock).expect("lock").expect("held");
    let started = Instant::now();
    let acquired =
        acquire_with(&harness.request(Some("http://127.0.0.1:9".into()))).expect("serve");
    assert!(started.elapsed() < Duration::from_millis(400));
    assert_eq!(acquired.bundle.expires_at, grant.expires_at);
}

#[test]
fn proactive_renewal_refreshes_when_lock_owned() {
    let harness = Harness::new();
    let mut cached = fixture_grant(PrincipalKind::Interactive);
    cached.issued_at = NOW - 100;
    cached.expires_at = NOW + 10;
    cached.deployment.token = deployment_token(&harness.home);
    cached = cached.with_checksum();
    let mut fresh = cached.clone();
    fresh.expires_at = NOW + 3_000;
    fresh = fresh.with_checksum();
    write_binding_for(&harness, "http://127.0.0.1:1", &cached.deployment.token);
    write_cache(&harness, &cached, None);
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(fresh.clone()),
        },
        Step::Config {
            revision: fresh.config_revision,
        },
    ]);
    write_binding_for(&harness, &scripted.url, &cached.deployment.token);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("renew");
    assert_eq!(acquired.bundle.expires_at, fresh.expires_at);
    let requests = join(scripted);
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.contains("POST /api/runtime/handshake HTTP"))
            .count(),
        1
    );
}

#[test]
fn handshake_retries_once_on_stale_epoch() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant = grant.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Reject {
            status: 409,
            body: "stale_epoch".into(),
        },
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config {
            revision: grant.config_revision,
        },
    ]);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("retry");
    assert_eq!(acquired.bundle.deployment.token, grant.deployment.token);
    let requests = join(scripted);
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.contains("POST /api/runtime/handshake HTTP"))
            .count(),
        2
    );
}

#[test]
fn presentation_http_classifies_retryable_restart_errors() {
    assert!(
        GrantError::from_presentation_http(409, "stale_epoch")
            .is_some_and(|error| error.is_retryable_presentation())
    );
    assert!(
        GrantError::from_presentation_http(401, "invalid_signature")
            .is_some_and(|error| error.is_retryable_presentation())
    );
    assert!(GrantError::from_presentation_http(401, "expired").is_none());
    assert!(
        GrantError::from_presentation_http(200, "revoked invalid_signature stale_epoch").is_none()
    );
}

#[test]
fn hmac_sha256_rejects_empty_key() {
    let error = super::handshake::hmac_sha256(b"", b"nonce").expect_err("empty key");
    assert!(matches!(error, GrantError::Malformed(_)));
}

#[test]
fn expected_schema_identity_tracks_catalog_head() {
    assert_eq!(expected_schema_identity().latest_version, 415);
}

#[test]
fn newer_generation_falls_back_to_fencing_epoch() {
    let mut existing = fixture_grant(PrincipalKind::Interactive);
    existing.capabilities.postgres = PostgresCapability::Unavailable {};
    existing.deployment.fencing_epoch = 4;
    let mut incoming = existing.clone();
    incoming.deployment.fencing_epoch = 3;
    assert!(!super::cache::newer_generation(Some(&existing), &incoming));
    incoming.deployment.fencing_epoch = 4;
    assert!(super::cache::newer_generation(Some(&existing), &incoming));
    incoming.deployment.fencing_epoch = 5;
    assert!(super::cache::newer_generation(Some(&existing), &incoming));
    assert!(super::cache::newer_generation(None, &incoming));
}

#[cfg(unix)]
#[test]
fn write_grant_file_creates_owner_only_mode() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().expect("dir");
    let path = dir.path().join("grant.json");
    write_grant_file(&path, &fixture_grant(PrincipalKind::Interactive)).expect("write");
    let mode = fs::metadata(&path).expect("meta").permissions().mode() & 0o777;
    assert_eq!(mode, 0o600);
}

#[test]
fn concurrent_renewal_refuses_downgrade() {
    let harness = Harness::new();
    let mut current = fixture_grant(PrincipalKind::Interactive);
    if let PostgresCapability::Direct {
        credential_generation,
        ..
    } = &mut current.capabilities.postgres
    {
        *credential_generation = 5;
    }
    current = current.with_checksum();
    write_binding_for(&harness, "http://127.0.0.1:1", &current.deployment.token);
    write_cache(&harness, &current, None);
    let path = interactive_cache_path(&harness.home, &current.deployment.token, PROJECT, None);

    let mut older = current.clone();
    if let PostgresCapability::Direct {
        credential_generation,
        ..
    } = &mut older.capabilities.postgres
    {
        *credential_generation = 4;
    }
    older = older.with_checksum();
    assert!(!super::cache::newer_generation(Some(&current), &older));

    let mut newer = current.clone();
    if let PostgresCapability::Direct {
        credential_generation,
        ..
    } = &mut newer.capabilities.postgres
    {
        *credential_generation = 6;
    }
    newer = newer.with_checksum();
    write_grant_file(&path, &newer).expect("newer wins");
    let loaded = load_grant_file(&path).expect("load");
    assert_eq!(loaded.credential_generation(), Some(6));
}

#[test]
fn bounded_renewal_contention() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.expires_at = NOW - 1;
    grant = grant.with_checksum();
    let listener = TcpListener::bind("127.0.0.1:0").expect("reachable daemon");
    let url = format!("http://{}", listener.local_addr().unwrap());
    write_binding_for(&harness, &url, &grant.deployment.token);
    write_cache(&harness, &grant, None);
    let lock_path = grant_lock_path(&interactive_cache_path(
        &harness.home,
        &grant.deployment.token,
        PROJECT,
        None,
    ));
    let _held = try_lock(&lock_path).expect("lock").expect("held");
    let mut request = harness.request(Some(url));
    request.deadline = Some(Duration::from_millis(80));
    let error = acquire_with(&request).expect_err("timeout");
    assert_eq!(error, GrantError::Timeout);
    drop(listener);

    drop(_held);
    fs::write(&lock_path, "1\n1\n").expect("stale lock");
    let mut fresh = grant.clone();
    fresh.expires_at = NOW + 3_000;
    fresh = fresh.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(fresh.clone()),
        },
        Step::Config {
            revision: fresh.config_revision,
        },
    ]);
    let mut request = harness.request(Some(scripted.url.clone()));
    request.stale_lock_after = Some(Duration::from_secs(0));
    let acquired = acquire_with(&request).expect("stale takeover");
    assert!(!acquired.bundle.is_expired(NOW));
    let _ = join(scripted);
}

#[test]
fn endpoint_deployment_binding() {
    let harness = Harness::new();
    let derived = deployment_token(&harness.home);
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = derived.clone();
    grant = grant.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config {
            revision: grant.config_revision,
        },
    ]);
    acquire_with(&harness.request(Some(scripted.url.clone()))).expect("bind");
    let _ = join(scripted);
    let binding = load_binding(&harness.home, "http://127.0.0.1:60887");
    assert!(binding.is_none() || binding.unwrap().deployment_token == derived);

    let mut other = grant.clone();
    other.deployment.token = "0123456789abcdef".into();
    other = other.with_checksum();
    write_cache(&harness, &other, None);
    write_binding_for(&harness, "http://127.0.0.1:1", "0123456789abcdef");
    let acquired =
        acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect("outage binding");
    assert_eq!(acquired.bundle.deployment.token, "0123456789abcdef");
}

#[test]
fn managed_refresh_envelope_auth() {
    let harness = Harness::new();
    let mut managed = fixture_grant(PrincipalKind::AgentRun);
    managed.expires_at = NOW - 1;
    managed = managed.with_checksum();
    let managed_path = harness.home.join("run.json");
    write_grant_file(&managed_path, &managed).unwrap();

    let mut request = harness.request(Some("http://127.0.0.1:9".into()));
    request.managed_bootstrap = Some(managed_path.clone());
    let error = acquire_with(&request).expect_err("no envelope");
    assert_eq!(error, GrantError::Expired);

    let expired = envelope_token(NOW - 10, PROJECT);
    request.managed_envelope = Some(expired.clone());
    let error = acquire_with(&request).expect_err("expired envelope");
    assert_eq!(error, GrantError::Expired);

    let mismatch = envelope_token(NOW + 60, "other-project");
    request.managed_envelope = Some(mismatch.clone());
    let error = acquire_with(&request).expect_err("mismatch");
    assert!(matches!(error, GrantError::Malformed(_)));
}

#[test]
fn config_revision_coherence() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant.config_revision = 2;
    grant = grant.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config { revision: 3 },
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config { revision: 2 },
    ]);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("coherent");
    assert_eq!(acquired.bundle.config_revision, 2);
    assert_eq!(
        acquired
            .settings
            .as_ref()
            .map(|settings| settings.config_revision),
        Some(2)
    );
    let _ = join(scripted);
}

#[test]
fn config_revision_second_mismatch_terminal() {
    let harness = Harness::new();
    let mut prior = fixture_grant(PrincipalKind::Interactive);
    prior.deployment.token = deployment_token(&harness.home);
    prior.config_revision = 1;
    prior.issued_at = NOW - 200;
    prior.expires_at = NOW - 1;
    prior = prior.with_checksum();
    write_cache(
        &harness,
        &prior,
        Some(&CachedSettings {
            config_revision: 1,
            settings: Default::default(),
        }),
    );
    let mut next = prior.clone();
    next.config_revision = 2;
    next = next.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(next.clone()),
        },
        Step::Config { revision: 3 },
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(next),
        },
        Step::Config { revision: 4 },
    ]);
    write_binding_for(&harness, &scripted.url, &prior.deployment.token);
    let error =
        acquire_with(&harness.request(Some(scripted.url.clone()))).expect_err("second mismatch");
    assert_eq!(error, GrantError::ConfigRevisionMismatch);
    let _ = join(scripted);
    let cached = load_grant_file(&interactive_cache_path(
        &harness.home,
        &prior.deployment.token,
        PROJECT,
        None,
    ))
    .expect("prior preserved");
    assert_eq!(cached.config_revision, 1);
}

#[test]
fn machine_config_is_grant_presented_and_not_capability() {
    let grant = fixture_grant(PrincipalKind::Interactive);
    let scripted = spawn_scripted(vec![Step::Config {
        revision: grant.config_revision,
    }]);
    let settings = fetch_runtime_config(&scripted.url, &grant, Some(TOKEN), Duration::from_secs(1))
        .expect("config");
    let requests = join(scripted);
    assert!(requests[0].contains("GET /api/runtime/config"));
    assert!(has_header(&requests[0], GRANT_HEADER));
    assert!(!settings.settings.contains_key("databases.postgres.dsn"));
    assert_eq!(settings.config_revision, grant.config_revision);
}

#[test]
fn managed_runtime_config_fetch_sends_identity_headers() {
    let grant = fixture_grant(PrincipalKind::AgentRun);
    let token = envelope_token(NOW + 60, PROJECT);
    let claims = parse_capability_token(&token).expect("claims");
    let scripted = spawn_scripted(vec![Step::Config {
        revision: grant.config_revision,
    }]);
    fetch_runtime_config(&scripted.url, &grant, Some(&token), Duration::from_secs(1))
        .expect("config");
    let requests = join(scripted);
    assert!(requests[0].contains("GET /api/runtime/config"));
    assert!(has_header(&requests[0], GRANT_HEADER));
    assert_eq!(
        header_value(&requests[0], CALLER_PROJECT_HEADER).as_deref(),
        Some(PROJECT)
    );
    assert_eq!(
        header_value(&requests[0], SESSION_HEADER).as_deref(),
        Some(claims.session_id.as_str())
    );
    assert_eq!(
        header_value(&requests[0], AGENT_RUN_HEADER).as_deref(),
        claims.agent_run_id.as_deref()
    );
    assert!(!has_header(&requests[0], MANAGED_EXECUTION_HEADER));
}

#[test]
fn operator_runtime_config_fetch_sends_no_identity_headers() {
    let grant = fixture_grant(PrincipalKind::Interactive);
    let scripted = spawn_scripted(vec![Step::Config {
        revision: grant.config_revision,
    }]);
    fetch_runtime_config(&scripted.url, &grant, Some(TOKEN), Duration::from_secs(1))
        .expect("config");
    let requests = join(scripted);
    assert!(!has_header(&requests[0], CALLER_PROJECT_HEADER));
    assert!(!has_header(&requests[0], SESSION_HEADER));
    assert!(!has_header(&requests[0], AGENT_RUN_HEADER));
    assert!(!has_header(&requests[0], MANAGED_EXECUTION_HEADER));
}

fn spawn_managed_challenge(token: &str, grant: GrantBundle) -> Scripted {
    let claims = parse_capability_token(token).expect("claims");
    let secret = claims.signature.clone();
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let url = format!("http://{}", listener.local_addr().unwrap());
    let requests = thread::spawn(move || {
        let mut captured = Vec::new();
        for step in 0..3 {
            let (mut stream, request) = loop {
                let Ok((mut stream, _)) = listener.accept() else {
                    return captured;
                };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                let request = read_http(&mut stream);
                if request.contains("HTTP/") {
                    break (stream, request);
                }
            };
            captured.push(request.clone());
            let (status, body) = if step == 0 {
                let nonce = request
                    .split("\"nonce\":\"")
                    .nth(1)
                    .and_then(|rest| rest.split('"').next())
                    .unwrap_or_default();
                let proof = hmac_hex(&secret, &b64url_decode(nonce));
                (200, json!({"proof": proof}).to_string())
            } else if step == 1 {
                (
                    200,
                    json!({
                        "grant": serde_json::to_value(&grant).unwrap(),
                        "deployment_token": grant.deployment.token,
                        "fencing_epoch": grant.deployment.fencing_epoch
                    })
                    .to_string(),
                )
            } else {
                (
                    200,
                    json!({
                        "config_revision": grant.config_revision,
                        "settings": {}
                    })
                    .to_string(),
                )
            };
            let _ = write!(
                stream,
                "HTTP/1.1 {status} OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
        }
        captured
    });
    Scripted { url, requests }
}

#[test]
fn refresh_destination_by_source_managed_writes_managed_file() {
    let harness = Harness::new();
    let interactive = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(
        &harness,
        "http://127.0.0.1:1",
        &interactive.deployment.token,
    );
    write_cache(&harness, &interactive, None);
    let cache_path =
        interactive_cache_path(&harness.home, &interactive.deployment.token, PROJECT, None);
    let cache_before = fs::read(&cache_path).unwrap();

    let mut managed = fixture_grant(PrincipalKind::AgentRun);
    managed.expires_at = NOW - 1;
    managed = managed.with_checksum();
    let managed_path = harness.home.join("run.json");
    write_grant_file(&managed_path, &managed).unwrap();
    let mut renewed = managed.clone();
    renewed.expires_at = NOW + 3_000;
    if let PostgresCapability::Direct {
        credential_generation,
        ..
    } = &mut renewed.capabilities.postgres
    {
        *credential_generation += 1;
    }
    renewed = renewed.with_checksum();
    let token = envelope_token(NOW + 60, PROJECT);
    let scripted = spawn_managed_challenge(&token, renewed.clone());
    let mut request = harness.request(Some(scripted.url.clone()));
    request.managed_bootstrap = Some(managed_path.clone());
    request.managed_envelope = Some(token.clone());
    let acquired = acquire_with(&request).expect("refresh");
    let requests = join(scripted);
    assert!(
        requests
            .iter()
            .any(|request| has_header(request, AUTHORIZATION_HEADER)
                && request.contains(&format!("Bearer {token}")))
    );
    assert!(
        !requests
            .iter()
            .any(|request| request.contains(&format!("Bearer {TOKEN}")))
    );
    assert_eq!(acquired.source, GrantSource::ManagedFile);
    assert_eq!(fs::read(&cache_path).unwrap(), cache_before);
    let written = load_grant_file(&managed_path).unwrap();
    assert_eq!(
        written.credential_generation(),
        renewed.credential_generation()
    );
}

#[test]
fn managed_refresh_uses_envelope_not_operator_on_wire() {
    let harness = Harness::new();
    let mut managed = fixture_grant(PrincipalKind::AgentRun);
    managed.expires_at = NOW - 1;
    managed = managed.with_checksum();
    let managed_path = harness.home.join("run.json");
    write_grant_file(&managed_path, &managed).unwrap();
    let mut renewed = managed.clone();
    renewed.expires_at = NOW + 3_000;
    renewed = renewed.with_checksum();
    let token = envelope_token(NOW + 60, PROJECT);
    let scripted = spawn_managed_challenge(&token, renewed);
    let mut request = harness.request(Some(scripted.url.clone()));
    request.managed_bootstrap = Some(managed_path.clone());
    request.managed_envelope = Some(token.clone());
    acquire_with(&request).expect("refresh");
    let requests = join(scripted);
    let handshake = requests
        .iter()
        .find(|request| request.contains("POST /api/runtime/handshake HTTP"))
        .expect("handshake");
    assert!(handshake.contains(&format!("Bearer {token}")));
    assert!(!handshake.contains(&format!("Bearer {TOKEN}")));
}

#[test]
fn managed_handshake_sends_identity_headers_matching_claims() {
    let harness = Harness::new();
    let mut managed = fixture_grant(PrincipalKind::AgentRun);
    managed.expires_at = NOW - 1;
    managed = managed.with_checksum();
    let managed_path = harness.home.join("run.json");
    write_grant_file(&managed_path, &managed).unwrap();
    let mut renewed = managed.clone();
    renewed.expires_at = NOW + 3_000;
    renewed = renewed.with_checksum();
    let token = envelope_token(NOW + 60, PROJECT);
    let claims = parse_capability_token(&token).expect("claims");
    let scripted = spawn_managed_challenge(&token, renewed);
    let mut request = harness.request(Some(scripted.url.clone()));
    request.managed_bootstrap = Some(managed_path);
    request.managed_envelope = Some(token.clone());
    acquire_with(&request).expect("refresh");
    let requests = join(scripted);

    // The daemon's capability matrix binds identity on the handshake route:
    // the caller-project, session, and owner headers must match the claims.
    let handshake = requests
        .iter()
        .find(|request| request.contains("POST /api/runtime/handshake HTTP"))
        .expect("handshake");
    assert_eq!(
        header_value(handshake, CALLER_PROJECT_HEADER).as_deref(),
        Some(claims.project_id.as_str())
    );
    assert_eq!(
        header_value(handshake, SESSION_HEADER).as_deref(),
        Some(claims.session_id.as_str())
    );
    assert_eq!(
        header_value(handshake, AGENT_RUN_HEADER),
        claims.agent_run_id
    );
    assert!(!has_header(handshake, MANAGED_EXECUTION_HEADER));

    // The challenge is a public pre-credential route and carries none of them.
    let challenge = requests
        .iter()
        .find(|request| request.contains("POST /api/runtime/handshake/challenge HTTP"))
        .expect("challenge");
    assert!(!has_header(challenge, CALLER_PROJECT_HEADER));
    assert!(!has_header(challenge, SESSION_HEADER));
    assert!(!has_header(challenge, AGENT_RUN_HEADER));
}

#[test]
#[serial_test::serial]
fn stale_epoch_single_retry() {
    let harness = Harness::new();
    let mut cached = fixture_grant(PrincipalKind::Interactive);
    cached.deployment.token = deployment_token(&harness.home);
    cached.deployment.fencing_epoch = 1;
    cached = cached.with_checksum();

    let mut fresh = cached.clone();
    fresh.deployment.fencing_epoch = 2;
    fresh = fresh.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(fresh.clone()),
        },
        Step::Config {
            revision: fresh.config_revision,
        },
    ]);
    write_binding_for(&harness, &scripted.url, &cached.deployment.token);
    write_cache(&harness, &cached, None);
    let mut request = harness.request(Some(scripted.url.clone()));
    let attempts = std::sync::atomic::AtomicUsize::new(0);
    let result = present_with_single_retry(&request, |grant| {
        let n = attempts.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        if n == 0 {
            assert_eq!(grant.deployment.fencing_epoch, 1);
            Err(GrantError::Malformed("stale_epoch".to_string()))
        } else {
            assert_eq!(grant.deployment.fencing_epoch, 2);
            Ok(grant.deployment.fencing_epoch)
        }
    });
    assert_eq!(result.expect("retry"), 2);
    assert_eq!(attempts.load(std::sync::atomic::Ordering::SeqCst), 2);
    let requests = join(scripted);
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.contains("POST /api/runtime/handshake HTTP"))
            .count(),
        1
    );

    let exhausted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(fresh.clone()),
        },
        Step::Config {
            revision: fresh.config_revision,
        },
    ]);
    request.daemon_url = Some(exhausted.url.clone());
    write_binding_for(&harness, &exhausted.url, &cached.deployment.token);
    write_cache(&harness, &cached, None);
    let attempts = std::sync::atomic::AtomicUsize::new(0);
    let error = present_with_single_retry(&request, |_grant| -> Result<(), GrantError> {
        attempts.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Err(GrantError::Malformed("stale_epoch".to_string()))
    })
    .expect_err("exhausted");
    assert!(error.is_stale_epoch() || matches!(error, GrantError::Malformed(_)));
    assert_eq!(attempts.load(std::sync::atomic::Ordering::SeqCst), 2);
    let requests = join(exhausted);
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.contains("POST /api/runtime/handshake HTTP"))
            .count(),
        1
    );
}

#[test]
fn write_coherent_pair_is_single_envelope() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    let settings = CachedSettings {
        config_revision: grant.config_revision,
        settings: Default::default(),
    };
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    write_coherent_pair(&path, &grant, &settings).expect("envelope");
    assert!(!settings_cache_path(&path).exists());
    let loaded = load_grant_file(&path).expect("unwrap grant");
    assert_eq!(loaded.config_revision, grant.config_revision);
    match super::cache::inspect_cache_pair(&path).expect("pair") {
        Some(super::cache::CachePair::Coherent(loaded_grant, loaded_settings)) => {
            assert_eq!(loaded_grant.config_revision, grant.config_revision);
            assert_eq!(loaded_settings.config_revision, grant.config_revision);
        }
        other => panic!("expected coherent envelope, got {other:?}"),
    }
}

#[test]
fn write_coherent_pair_rejects_revision_mismatch() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    let settings = CachedSettings {
        config_revision: grant.config_revision + 1,
        settings: Default::default(),
    };
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    let error = write_coherent_pair(&path, &grant, &settings).expect_err("mismatch");
    assert_eq!(error, GrantError::ConfigRevisionMismatch);
    assert!(!path.exists());
}

#[test]
fn torn_two_file_cache_is_not_served_offline() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.config_revision = 2;
    grant = grant.with_checksum();
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    write_torn_two_file_cache(
        &harness,
        &grant,
        &CachedSettings {
            config_revision: 1,
            settings: [("ai.embeddings.model".into(), "stale".into())]
                .into_iter()
                .collect(),
        },
    );
    let acquired =
        acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect("grant only");
    assert_eq!(acquired.bundle.config_revision, 2);
    assert_eq!(acquired.settings, None);
}

#[test]
fn torn_two_file_cache_rehandshakes_when_reachable() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant.config_revision = 2;
    grant = grant.with_checksum();
    write_torn_two_file_cache(
        &harness,
        &grant,
        &CachedSettings {
            config_revision: 1,
            settings: Default::default(),
        },
    );
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config { revision: 2 },
    ]);
    write_binding_for(&harness, &scripted.url, &grant.deployment.token);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("rehandshake");
    let _ = join(scripted);
    assert_eq!(acquired.source, GrantSource::Handshake);
    assert_eq!(
        acquired
            .settings
            .as_ref()
            .map(|settings| settings.config_revision),
        Some(2)
    );
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    assert!(!settings_cache_path(&path).exists());
    match super::cache::inspect_cache_pair(&path).expect("pair") {
        Some(super::cache::CachePair::Coherent(loaded_grant, loaded_settings)) => {
            assert_eq!(loaded_grant.config_revision, 2);
            assert_eq!(loaded_settings.config_revision, 2);
        }
        other => panic!("expected coherent envelope, got {other:?}"),
    }
}

#[test]
fn handshake_persist_does_not_leave_settings_sibling() {
    let harness = Harness::new();
    let mut grant = fixture_grant(PrincipalKind::Interactive);
    grant.deployment.token = deployment_token(&harness.home);
    grant = grant.with_checksum();
    let scripted = spawn_scripted(vec![
        Step::Challenge {
            valid: true,
            token: TOKEN.into(),
        },
        Step::Handshake {
            grant: Box::new(grant.clone()),
        },
        Step::Config {
            revision: grant.config_revision,
        },
    ]);
    let acquired = acquire_with(&harness.request(Some(scripted.url.clone()))).expect("handshake");
    let _ = join(scripted);
    assert_eq!(acquired.source, GrantSource::Handshake);
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    assert!(!settings_cache_path(&path).exists());
    assert!(matches!(
        super::cache::inspect_cache_pair(&path).expect("pair"),
        Some(super::cache::CachePair::Coherent(_, _))
    ));
}

#[test]
fn maintenance_principal_is_managed_and_serializes() {
    assert!(PrincipalKind::Maintenance.is_managed());
    let grant = fixture_grant(PrincipalKind::Maintenance);
    assert_eq!(grant.principal.kind, PrincipalKind::Maintenance);
    assert_eq!(grant.principal.execution_id.as_deref(), Some("exec-1"));
    let value = serde_json::to_value(&grant.principal).expect("serialize");
    assert_eq!(value["kind"], "maintenance");
    let claims = CapabilityClaims {
        session_id: "exec-1".into(),
        project_id: PROJECT.into(),
        machine_id: MACHINE.into(),
        iat: NOW,
        exp: NOW + 60,
        agent_run_id: None,
        managed_execution_id: Some("exec-1".into()),
        kind: Some(PrincipalKind::Maintenance),
        signature: vec![0; 32],
    };
    assert!(claims.matches_principal(&grant.principal));
    let tool_claims = CapabilityClaims {
        session_id: "exec-1".into(),
        project_id: PROJECT.into(),
        machine_id: MACHINE.into(),
        iat: NOW,
        exp: NOW + 60,
        agent_run_id: None,
        managed_execution_id: Some("exec-1".into()),
        kind: Some(PrincipalKind::ToolChat),
        signature: vec![0; 32],
    };
    assert!(!tool_claims.matches_principal(&grant.principal));
}

fn grant_json_without_api_contract(grant: &GrantBundle) -> Vec<u8> {
    let mut value = serde_json::to_value(grant).expect("json");
    value
        .as_object_mut()
        .expect("object")
        .remove("api_contract");
    serde_json::to_vec(&value).expect("serialize")
}

fn grant_json_with_unknown_field(grant: &GrantBundle) -> Vec<u8> {
    let mut value = serde_json::to_value(grant).expect("json");
    value
        .as_object_mut()
        .expect("object")
        .insert("credential_generation".into(), json!(1));
    serde_json::to_vec(&value).expect("serialize")
}

#[test]
fn managed_grant_file_parse_failure_names_source() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::AgentRun);
    let path = harness.home.join("managed.json");
    fs::write(&path, grant_json_without_api_contract(&grant)).expect("write");
    let mut request = harness.request(Some("http://127.0.0.1:1".into()));
    request.managed_bootstrap = Some(path.clone());
    let error = acquire_with(&request).expect_err("managed parse");
    let source = format!("managed grant file {}", path.display());
    assert_eq!(
        error,
        GrantError::ApiContractMismatch {
            grant_contract: None,
            binary_contract: EXPECTED_API_CONTRACT,
            source: Some(source.clone()),
        }
    );
    assert!(
        error.to_string().starts_with(&format!("{source}:")),
        "display must name the managed file, got {error}"
    );
}

#[test]
fn interactive_cache_parse_failure_names_source() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(&harness, "http://127.0.0.1:1", &grant.deployment.token);
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    fs::create_dir_all(path.parent().expect("parent")).expect("dirs");
    fs::write(&path, grant_json_with_unknown_field(&grant)).expect("write");
    let error =
        acquire_with(&harness.request(Some("http://127.0.0.1:1".into()))).expect_err("cache parse");
    let source = format!("interactive grant cache {}", path.display());
    match error {
        GrantError::PayloadSkew { detail } => {
            assert!(
                detail.starts_with(&format!("{source}:")),
                "detail must name the cache file, got {detail}"
            );
            assert!(
                detail.contains("credential_generation") || detail.contains("unknown field"),
                "detail must keep the inner serde cause, got {detail}"
            );
        }
        other => panic!("expected PayloadSkew, got {other:?}"),
    }
}

#[test]
fn handshake_parse_failure_names_source() {
    let grant = fixture_grant(PrincipalKind::Interactive);
    let body = json!({
        "grant": serde_json::from_slice::<Value>(&grant_json_without_api_contract(&grant))
            .expect("value")
    })
    .to_string();
    let error = super::handshake::grant_from_handshake(super::handshake::HttpResponse {
        status: 200,
        body,
    })
    .expect_err("handshake parse");
    assert_eq!(
        error,
        GrantError::ApiContractMismatch {
            grant_contract: None,
            binary_contract: EXPECTED_API_CONTRACT,
            source: Some("daemon handshake response".into()),
        }
    );
}

#[test]
fn refresh_or_fail_reread_names_cached_grant_source() {
    let harness = Harness::new();
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let url = format!("http://{}", listener.local_addr().expect("addr"));
    let existing = fixture_grant(PrincipalKind::Interactive);
    let dest = harness.home.join("stale-cache.json");
    fs::write(&dest, grant_json_with_unknown_field(&existing)).expect("write");
    let ctx = super::AcquireCtx::from_request(&harness.request(Some(url))).expect("ctx");
    let error = super::refresh_or_fail(
        &ctx,
        Some(&existing),
        GrantSource::Cache,
        dest.clone(),
        false,
        true,
        None,
    )
    .expect_err("reread parse");
    drop(listener);
    let source = format!("cached grant {}", dest.display());
    match error {
        GrantError::PayloadSkew { detail } => {
            assert!(
                detail.starts_with(&format!("{source}:")),
                "detail must name the cached grant, got {detail}"
            );
        }
        other => panic!("expected PayloadSkew, got {other:?}"),
    }
}

#[test]
fn envelope_cache_with_skewed_grant_reports_inner_typed_error() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    let inner =
        serde_json::from_slice::<Value>(&grant_json_with_unknown_field(&grant)).expect("inner");
    let envelope = json!({
        "grant": inner,
        "settings": {
            "config_revision": grant.config_revision,
            "settings": {}
        }
    });
    let path = harness.home.join("envelope.json");
    fs::write(&path, serde_json::to_vec(&envelope).expect("serialize")).expect("write");
    let error = load_grant_file(&path).expect_err("envelope skew");
    match error {
        GrantError::PayloadSkew { detail } => {
            assert!(
                detail.contains("unknown field") && detail.contains("credential_generation"),
                "must report the inner grant's unknown field, got {detail}"
            );
            assert!(
                !detail.contains("unknown field `grant`")
                    && !detail.contains("unknown field `settings`"),
                "must not reparse the envelope as a bare grant, got {detail}"
            );
        }
        other => panic!("expected inner PayloadSkew, got {other:?}"),
    }
}

#[test]
fn inspect_cached_grant_reports_malformed_reason() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(&harness, "http://127.0.0.1:60887", &grant.deployment.token);
    let path = interactive_cache_path(&harness.home, &grant.deployment.token, PROJECT, None);
    fs::create_dir_all(path.parent().expect("parent")).expect("dirs");
    fs::write(&path, grant_json_with_unknown_field(&grant)).expect("write");
    let inspected = inspect_cached_grant_at(
        &harness.project_root,
        Some(&harness.home),
        Some("http://127.0.0.1:60887"),
        Some(NOW),
    );
    match inspected {
        CachedGrantInspection::Malformed { reason } => {
            assert!(!reason.is_empty(), "malformed inspection must explain why");
            assert!(
                reason.contains("payload skew") || reason.contains("unknown field"),
                "reason must surface the parse failure, got {reason}"
            );
        }
        other => panic!("expected Malformed {{ reason }}, got {other:?}"),
    }
}

#[test]
fn inspect_cached_grant_missing_file_is_absent() {
    let harness = Harness::new();
    let grant = fixture_grant(PrincipalKind::Interactive);
    write_binding_for(&harness, "http://127.0.0.1:60887", &grant.deployment.token);
    let inspected = inspect_cached_grant_at(
        &harness.project_root,
        Some(&harness.home),
        Some("http://127.0.0.1:60887"),
        Some(NOW),
    );
    assert!(matches!(inspected, CachedGrantInspection::Absent));
}

// Changing this inventory requires bumping EXPECTED_API_CONTRACT (Rust) and
// API_CONTRACT (Python) together and regenerating the goldens.
const GRANT_FIELD_INVENTORY: &[&str] = &[
    "api_contract",
    "capabilities",
    "config_revision",
    "deployment",
    "expires_at",
    "issued_at",
    "payload_checksum",
    "postgres.credential_generation",
    "postgres.dsn",
    "postgres.mode",
    "postgres.role_name",
    "postgres.valid_until",
    "principal",
    "schema_identity",
    "signature",
    "version",
];

fn serialized_field_inventory(value: &Value) -> Vec<String> {
    let mut names: Vec<String> = value.as_object().expect("object").keys().cloned().collect();
    let postgres = value
        .pointer("/capabilities/postgres")
        .and_then(Value::as_object)
        .expect("postgres");
    names.extend(postgres.keys().map(|key| format!("postgres.{key}")));
    names.sort();
    names
}

#[test]
fn payload_skew_unknown_field_golden_is_payload_skew() {
    let raw = fs::read(golden_dir().join("payload_skew_unknown_field.json"))
        .expect("payload_skew_unknown_field.json");
    let value: Value = serde_json::from_slice(raw.trim_ascii()).expect("json");
    assert_eq!(value["api_contract"], EXPECTED_API_CONTRACT);
    assert_eq!(value["future_capability_probe"], 1);
    let error = parse_grant_json(&raw).expect_err("skew");
    assert!(
        matches!(error, GrantError::PayloadSkew { .. }),
        "expected PayloadSkew, got {error:?}"
    );
    assert_eq!(error.cli_code(), "payload_skew");
}

#[test]
fn grant_field_inventory_matches_expected_api_contract() {
    let (_, grant) = load_golden("direct_datastores.json");
    assert_eq!(grant.api_contract, EXPECTED_API_CONTRACT);
    let value = serde_json::to_value(&grant).expect("json");
    let actual = serialized_field_inventory(&value);
    assert_eq!(actual, GRANT_FIELD_INVENTORY);
}
