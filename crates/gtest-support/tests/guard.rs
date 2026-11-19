//! Verdict tests for the DATABASE_URL guard, driven by the shared vector file that the Python
//! guard also reads (design-v2 section 7.1.4). Each vector gets the verdict and rule it names.

use std::collections::HashMap;
use std::fs;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
#[cfg(unix)]
use std::process::Command;

use serde_json::{Value, json};

use gobby_test_support::hub_guard::{
    DbUrlError, FIXTURE_DATABASE_PREFIX, GuardMode, HARNESS_DATABASE_PREFIX, HubGuardError,
    SHARED_PORTS, check_derived_url_with_env, check_maintenance_url, check_test_hub,
    database_url_with_env, derive_run_url, harness_database_name, maintenance_url, operator_home,
    run_database_pid,
};

const VECTORS: &str = include_str!("../../../tests/fixtures/hub_guard_vectors.json");
const TEST_HUB_URL: &str = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test";

fn document() -> Value {
    serde_json::from_str(VECTORS).expect("hub_guard_vectors.json parses")
}

fn vectors(document: &Value) -> &Vec<Value> {
    document["vectors"].as_array().expect("vectors array")
}

fn env_map(spec: &Value) -> HashMap<String, String> {
    spec.as_object()
        .expect("env object")
        .iter()
        .map(|(name, value)| (name.clone(), value.as_str().expect("env value").to_owned()))
        .collect()
}

fn mode_name(mode: GuardMode) -> &'static str {
    match mode {
        GuardMode::TestHub => "test_hub",
        GuardMode::Rehearsal => "rehearsal",
    }
}

/// Write a vector's rehearsal profile under `dir` and return the path to pass (the design's
/// `_materialize_profile`). Git does not store modes, so the test sets them here.
pub(super) fn materialize_profile(spec: &Value, dir: &Path) -> Option<PathBuf> {
    if spec.is_null() {
        return None;
    }
    let home = match spec["gobby_home"].as_str().expect("gobby_home") {
        "<operator_home>" => operator_home().expect("operator home"),
        _ => dir.join("gobby-home"),
    };
    let body = if spec["content"] == "text" {
        "not json\n".to_owned()
    } else {
        json!({
            "gobby_home": home.display().to_string(),
            "postgres_host": spec["postgres_host"],
            "postgres_system_identifier": spec["postgres_system_identifier"],
            "services": {"postgres": {"port": spec["services_postgres_port"]}},
        })
        .to_string()
    };
    let target = dir.join("rehearsal-profile.json");
    fs::write(&target, body).expect("write profile");
    #[cfg(unix)]
    {
        let mode =
            u32::from_str_radix(spec["mode"].as_str().expect("mode"), 8).expect("octal mode");
        fs::set_permissions(&target, fs::Permissions::from_mode(mode)).expect("chmod profile");
    }
    match spec["path"].as_str().expect("path") {
        "absolute" => Some(target),
        "relative" => Some(PathBuf::from("relative").join("rehearsal-profile.json")),
        "symlink" => {
            // Windows symlinks need a privilege plain tests cannot assume. Without the link the
            // path is missing, which the guard refuses the same way (R4), so the vector holds.
            let link = dir.join("profile-link.json");
            #[cfg(unix)]
            std::os::unix::fs::symlink(&target, &link).expect("symlink profile");
            Some(link)
        }
        other => panic!("unknown profile path kind {other:?}"),
    }
}

/// The verdict a vector names: `accept:<mode>` or `refuse:<rule>`.
fn expected_verdict(vector: &Value) -> String {
    if vector["expect"] == "accept" {
        format!("accept:{}", vector["mode"].as_str().expect("mode"))
    } else {
        format!("refuse:{}", vector["rule"].as_str().expect("rule"))
    }
}

/// On Windows the owner and 0600 checks cannot run (`open_checked_profile`), so an absolute
/// rehearsal profile that reaches them is refused with the distinct Windows rule instead of its
/// Unix verdict. Other vectors keep the Python verdict on every platform.
#[cfg(windows)]
fn windows_adjusted(vector: &Value, expected: String) -> String {
    let reaches_owner_check = vector["op"] == "check" && vector["profile"]["path"] == "absolute";
    if reaches_owner_check && (expected == "accept:rehearsal" || expected == "refuse:R4") {
        "refuse:R4-windows".to_owned()
    } else {
        expected
    }
}

#[cfg(unix)]
fn windows_adjusted(_vector: &Value, expected: String) -> String {
    expected
}

/// The verdict this engine returns for a vector, in the same form.
fn actual_verdict(vector: &Value, dir: &Path) -> String {
    let env = env_map(&vector["env"]);
    let lookup = |name: &str| env.get(name).cloned();
    let outcome: Result<String, HubGuardError> = match vector["op"].as_str().expect("op") {
        "check" => {
            let profile = materialize_profile(&vector["profile"], dir);
            check_test_hub(vector["url"].as_str(), &lookup, profile.as_deref())
                .map(|guard| format!("accept:{}", mode_name(guard.mode)))
        }
        "derive" => {
            let pid = u32::try_from(vector["pid"].as_u64().expect("pid")).expect("pid fits u32");
            check_test_hub(vector["url"].as_str(), &lookup, None).and_then(|guard| {
                let name =
                    harness_database_name(pid, vector["run_suffix"].as_str().expect("run_suffix"))?;
                let derived = derive_run_url(&guard, &name, None)?;
                check_derived_url_with_env(&derived, &name, None, &|_| None)?;
                if name != vector["derived_dbname"] {
                    return Ok(format!("derived:{name}"));
                }
                Ok(format!("accept:{}", mode_name(guard.mode)))
            })
        }
        "check_derived" => check_derived_url_with_env(
            vector["url"].as_str().expect("derived url"),
            vector["expected_dbname"].as_str().expect("expected_dbname"),
            vector["expected_schema"].as_str(),
            &lookup,
        )
        .map(|()| "accept:test_hub".to_owned()),
        other => panic!("unknown vector op {other:?}"),
    };
    match outcome {
        Ok(verdict) => verdict,
        Err(error) => format!("refuse:{}", error.rule),
    }
}

#[test]
fn every_vector_gets_the_python_verdict_and_rule() {
    let document = document();
    let scratch = tempfile::tempdir().expect("scratch dir");
    let mut mismatches = Vec::new();
    for vector in vectors(&document) {
        let id = vector["id"].as_str().expect("vector id");
        let dir = scratch.path().join(id);
        fs::create_dir_all(&dir).expect("vector dir");
        let expected = windows_adjusted(vector, expected_verdict(vector));
        let actual = actual_verdict(vector, &dir);
        if actual != expected {
            mismatches.push(format!("{id}: expected {expected}, got {actual}"));
        }
    }
    assert!(
        mismatches.is_empty(),
        "verdict mismatches:\n{}",
        mismatches.join("\n")
    );
}

#[test]
fn vector_file_is_schema_one_with_unique_ids_and_known_fields() {
    let document = document();
    assert_eq!(document["schema_version"], 1);
    let mut ids: Vec<&str> = vectors(&document)
        .iter()
        .map(|vector| vector["id"].as_str().expect("id"))
        .collect();
    let count = ids.len();
    ids.sort_unstable();
    ids.dedup();
    assert_eq!(ids.len(), count, "vector ids are unique");
    for vector in vectors(&document) {
        let op = vector["op"].as_str().expect("op");
        assert!(
            matches!(op, "check" | "derive" | "check_derived"),
            "op {op:?}"
        );
        match vector["expect"].as_str().expect("expect") {
            "accept" => assert_eq!(vector["rule"], "", "accept carries no rule"),
            "refuse" => assert_eq!(vector["mode"], "", "refuse carries no mode"),
            other => panic!("expect {other:?}"),
        }
    }
}

#[test]
fn shared_ports_copy_equals_the_vector_file() {
    let document = document();
    let mut from_file: Vec<u64> = document["shared_ports"]
        .as_array()
        .expect("shared_ports")
        .iter()
        .map(|port| port.as_u64().expect("port"))
        .collect();
    let mut copy: Vec<u64> = SHARED_PORTS.iter().map(|port| u64::from(*port)).collect();
    from_file.sort_unstable();
    copy.sort_unstable();
    assert_eq!(copy, from_file);
}

#[test]
fn derived_url_replaces_only_the_database_path() {
    let base = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test?sslmode=disable";
    let guard = check_test_hub(Some(base), &|_| None, None).expect("base accepted");
    let name = "gobby_harness_4242_0123456789ab";
    assert_eq!(
        derive_run_url(&guard, name, None).expect("derived"),
        format!("postgresql://gobby_test:gobby_test@127.0.0.1:60892/{name}?sslmode=disable")
    );
    assert_eq!(
        derive_run_url(&guard, name, Some("run_schema")).expect("derived with schema"),
        format!(
            "postgresql://gobby_test:gobby_test@127.0.0.1:60892/{name}?sslmode=disable&options=-csearch_path%3Drun_schema"
        )
    );
}

#[test]
fn derivation_refuses_names_outside_the_run_patterns_and_bad_schemas() {
    let guard = check_test_hub(Some(TEST_HUB_URL), &|_| None, None).expect("base accepted");
    assert_eq!(
        derive_run_url(&guard, "gobby", None)
            .expect_err("live-looking name")
            .rule,
        "ARG"
    );
    let name = "gobby_harness_4242_0123456789ab";
    assert_eq!(
        derive_run_url(&guard, name, Some("run-schema"))
            .expect_err("schema with a dash")
            .rule,
        "ARG"
    );
}

// Accepts a rehearsal profile, which needs the Unix owner and 0600 checks: Unix only (the Windows
// refusal is covered by the vector verdicts above).
#[cfg(unix)]
#[test]
fn rehearsal_derivation_returns_the_base_unchanged() {
    let dir = tempfile::tempdir().expect("scratch dir");
    let spec = json!({
        "path": "absolute", "mode": "0600", "gobby_home": "<other>",
        "postgres_host": "127.0.0.1", "postgres_system_identifier": "7000000000000000001",
        "services_postgres_port": 61500, "content": "json",
    });
    let profile = materialize_profile(&spec, dir.path());
    let url = "postgresql://gobby_test:gobby_test@127.0.0.1:61500/gobby_test";
    let guard =
        check_test_hub(Some(url), &|_| None, profile.as_deref()).expect("rehearsal accepted");
    assert_eq!(guard.mode, GuardMode::Rehearsal);
    assert_eq!(
        derive_run_url(&guard, "gobby_harness_4242_0123456789ab", None).expect("base"),
        url
    );
}

#[test]
fn maintenance_url_names_the_postgres_database() {
    assert_eq!(
        maintenance_url(TEST_HUB_URL),
        "postgresql://gobby_test:gobby_test@127.0.0.1:60892/postgres"
    );
}

#[test]
fn sweep_connection_must_be_a_loopback_test_user_on_postgres() {
    check_maintenance_url("postgresql://gobby_test:gobby_test@127.0.0.1:60892/postgres")
        .expect("loopback test user on postgres accepted");
    let remote =
        check_maintenance_url("postgresql://gobby_test:gobby_test@10.0.0.5:60892/postgres")
            .expect_err("non-loopback endpoint");
    assert_eq!(remote.rule, "R3");
    let live_user = check_maintenance_url("postgresql://gobby:gobby@127.0.0.1:60892/postgres")
        .expect_err("user other than gobby_test");
    assert_eq!(live_user.rule, "R3");
    let wrong_path = check_maintenance_url(TEST_HUB_URL).expect_err("not the maintenance database");
    assert_eq!(wrong_path.rule, "ARG");
}

#[test]
fn run_database_names_match_only_the_whole_pattern() {
    assert_eq!(
        run_database_pid("gobby_harness_4242_0123456789ab", HARNESS_DATABASE_PREFIX),
        Some(4242)
    );
    assert_eq!(
        run_database_pid("gobby_test_4242_0123456789ab", FIXTURE_DATABASE_PREFIX),
        Some(4242)
    );
    for name in [
        "gobby_harness_0_0123456789ab",
        "gobby_harness_04242_0123456789ab",
        "gobby_harness_12345678_0123456789ab",
        "gobby_harness_4242_0123456789AB",
        "gobby_harness_4242_0123456789abc",
        "gobby_harness_4242_0123456789a",
        "xgobby_harness_4242_0123456789ab",
        "gobby_test_4242_0123456789ab",
        "gobby",
    ] {
        assert_eq!(
            run_database_pid(name, HARNESS_DATABASE_PREFIX),
            None,
            "{name} matches the harness pattern"
        );
    }
}

#[test]
fn harness_names_follow_the_pattern() {
    assert_eq!(
        harness_database_name(4242, "0123456789ab").expect("name"),
        "gobby_harness_4242_0123456789ab"
    );
    assert_eq!(
        harness_database_name(0, "0123456789ab")
            .expect_err("pid 0")
            .rule,
        "ARG"
    );
    assert_eq!(
        harness_database_name(4242, "0123456789AB")
            .expect_err("uppercase")
            .rule,
        "ARG"
    );
}

#[test]
fn database_url_for_tests_accepts_any_loopback_test_hub_port() {
    let unset = |_: &str| None;
    assert_eq!(
        database_url_with_env(&unset).expect_err("unset"),
        DbUrlError::Unset
    );
    let url = "postgresql://gobby_test:gobby_test@localhost:61500/gobby_test";
    let only_url = |name: &str| (name == "DATABASE_URL").then(|| url.to_owned());
    assert_eq!(
        database_url_with_env(&only_url).expect("any loopback port"),
        url
    );
    let live = "postgresql://gobby:gobby@localhost:60891/gobby";
    let live_env = |name: &str| (name == "DATABASE_URL").then(|| live.to_owned());
    match database_url_with_env(&live_env).expect_err("live hub") {
        DbUrlError::Refused(error) => assert_eq!(error.rule, "R3"),
        DbUrlError::Unset => panic!("live hub reported as unset"),
    }
    let empty_env = |name: &str| (name == "DATABASE_URL").then(String::new);
    match database_url_with_env(&empty_env).expect_err("empty") {
        DbUrlError::Refused(error) => assert_eq!(error.rule, "ARG"),
        DbUrlError::Unset => panic!("empty reported as unset"),
    }
    let redirected = |name: &str| match name {
        "DATABASE_URL" => Some(TEST_HUB_URL.to_owned()),
        "PGSERVICE" => Some("x".to_owned()),
        _ => None,
    };
    match database_url_with_env(&redirected).expect_err("redirected") {
        DbUrlError::Refused(error) => assert_eq!(error.rule, "R2"),
        DbUrlError::Unset => panic!("redirected reported as unset"),
    }
}

const HOME_PROBE_PROFILE_ENV: &str = "GOBBY_GUARD_HOME_PROBE_PROFILE";
const HOME_PROBE_OUT_ENV: &str = "GOBBY_GUARD_HOME_PROBE_OUT";
/// A loopback test hub URL on a port that is not shared, so the rehearsal profile can name it.
const REHEARSAL_URL: &str = "postgresql://gobby_test:gobby_test@127.0.0.1:61503/gobby_test";

/// Child half of `home_override_does_not_change_the_rehearsal_verdict`. It runs only when the
/// parent sets both probe variables, and it writes the guard's verdict for the profile to a file.
#[test]
fn home_probe_child() {
    let (Some(profile), Some(out)) = (
        std::env::var_os(HOME_PROBE_PROFILE_ENV),
        std::env::var_os(HOME_PROBE_OUT_ENV),
    ) else {
        return;
    };
    let verdict = match check_test_hub(Some(REHEARSAL_URL), &|_| None, Some(Path::new(&profile))) {
        Ok(guard) => format!("accept:{}", mode_name(guard.mode)),
        Err(error) => format!("refuse:{}", error.rule),
    };
    fs::write(out, verdict).expect("write the probe verdict");
}

/// A rehearsal profile whose gobby_home is `<HOME>/.gobby` is refused as the operator's home only
/// when HOME points at that directory. The operator home comes from the passwd entry, so a child
/// process whose HOME is overridden must return the same verdict as the parent.
// Unix only: the passwd home it checks is the Unix lookup. On Windows the home comes from
// USERPROFILE, an environment value, so an override there is not a property the guard keeps.
#[cfg(unix)]
#[test]
fn home_override_does_not_change_the_rehearsal_verdict() {
    let dir = tempfile::tempdir().expect("tempdir");
    let fake_home = dir.path().join("fake-home");
    fs::create_dir_all(fake_home.join(".gobby")).expect("fake home gobby directory");
    let profile = dir.path().join("rehearsal-profile.json");
    let body = json!({
        "gobby_home": fake_home.join(".gobby").display().to_string(),
        "postgres_host": "127.0.0.1",
        "postgres_system_identifier": "7000000000000000001",
        "services": {"postgres": {"port": 61503}},
    });
    fs::write(&profile, body.to_string()).expect("write profile");
    fs::set_permissions(&profile, fs::Permissions::from_mode(0o600)).expect("chmod profile");

    let parent = check_test_hub(Some(REHEARSAL_URL), &|_| None, Some(&profile))
        .map(|guard| format!("accept:{}", mode_name(guard.mode)))
        .unwrap_or_else(|error| format!("refuse:{}", error.rule));
    assert_eq!(
        parent, "accept:rehearsal",
        "the parent process accepts the profile"
    );

    let out = dir.path().join("child-verdict.txt");
    let status = Command::new(std::env::current_exe().expect("this test binary"))
        .args(["--exact", "guard::home_probe_child"])
        .env("HOME", &fake_home)
        .env(HOME_PROBE_PROFILE_ENV, &profile)
        .env(HOME_PROBE_OUT_ENV, &out)
        .status()
        .expect("run the probe child");
    assert!(status.success(), "the probe child failed: {status}");
    let child = fs::read_to_string(&out).expect("the probe child wrote its verdict");
    assert_eq!(
        child, parent,
        "overriding HOME must not change the operator-home verdict"
    );
}
