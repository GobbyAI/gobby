use std::path::{Path, PathBuf};
use std::process::Command;

use gobby_core::runtime_mode::{RUNTIME_MODE_ENV, RuntimeMode, runtime_mode};

const CHILD_SCENARIO_ENV: &str = "GOBBY_RUNTIME_MODE_TEST_SCENARIO";
const DAEMON_URL_ENV: &str = "GOBBY_DAEMON_URL";

#[test]
fn process_lifetime_selection_is_immutable() {
    if let Some(scenario) = std::env::var_os(CHILD_SCENARIO_ENV) {
        run_child_scenario(scenario.to_str().expect("UTF-8 scenario"));
        return;
    }

    let mut scenarios = vec!["environment_standalone", "environment_daemon"];
    if registration_path(Path::new("/tmp")).is_some() {
        scenarios.extend(["registration_standalone", "registration_daemon"]);
    }

    let executable = std::env::current_exe().expect("current test executable");
    for scenario in scenarios {
        let output = Command::new(&executable)
            .args([
                "--exact",
                "process_lifetime_selection_is_immutable",
                "--nocapture",
            ])
            .env(CHILD_SCENARIO_ENV, scenario)
            .output()
            .expect("run isolated runtime-mode scenario");
        assert!(
            output.status.success(),
            "scenario {scenario} failed\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

fn run_child_scenario(scenario: &str) {
    let root = tempfile::tempdir().expect("isolated runtime-mode home");
    isolate_service_paths(root.path());
    unsafe {
        std::env::remove_var(DAEMON_URL_ENV);
    }

    match scenario {
        "environment_standalone" => {
            unsafe {
                std::env::set_var(RUNTIME_MODE_ENV, "standalone");
            }
            assert_eq!(
                runtime_mode().expect("prime standalone"),
                RuntimeMode::Standalone
            );
            unsafe {
                std::env::set_var(RUNTIME_MODE_ENV, "auto");
                std::env::set_var(DAEMON_URL_ENV, "https://daemon.example");
            }
            assert_eq!(
                runtime_mode().expect("cached standalone"),
                RuntimeMode::Standalone
            );
        }
        "environment_daemon" => {
            unsafe {
                std::env::set_var(RUNTIME_MODE_ENV, "auto");
                std::env::set_var(DAEMON_URL_ENV, "https://daemon.example");
            }
            assert_eq!(runtime_mode().expect("prime daemon"), RuntimeMode::Daemon);
            unsafe {
                std::env::set_var(RUNTIME_MODE_ENV, "standalone");
                std::env::remove_var(DAEMON_URL_ENV);
            }
            assert_eq!(runtime_mode().expect("cached daemon"), RuntimeMode::Daemon);
        }
        "registration_standalone" => {
            unsafe {
                std::env::set_var(RUNTIME_MODE_ENV, "auto");
            }
            let registration = registration_path(root.path()).expect("supported platform");
            assert_eq!(
                runtime_mode().expect("prime standalone"),
                RuntimeMode::Standalone
            );
            write_registration(&registration);
            assert_eq!(
                runtime_mode().expect("cached standalone"),
                RuntimeMode::Standalone
            );
        }
        "registration_daemon" => {
            unsafe {
                std::env::set_var(RUNTIME_MODE_ENV, "auto");
            }
            let registration = registration_path(root.path()).expect("supported platform");
            write_registration(&registration);
            assert_eq!(runtime_mode().expect("prime daemon"), RuntimeMode::Daemon);
            std::fs::remove_file(registration).expect("remove registration");
            assert_eq!(runtime_mode().expect("cached daemon"), RuntimeMode::Daemon);
        }
        other => panic!("unknown child scenario {other}"),
    }
}

fn isolate_service_paths(root: &Path) {
    unsafe {
        std::env::set_var("HOME", root);
        std::env::set_var("USERPROFILE", root);
        std::env::set_var("XDG_CONFIG_HOME", root.join("xdg"));
        std::env::set_var("GOBBY_HOME", root.join("gobby"));
    }
}

fn registration_path(root: &Path) -> Option<PathBuf> {
    if cfg!(target_os = "macos") {
        Some(root.join("Library/LaunchAgents/com.gobby.daemon.plist"))
    } else if cfg!(target_os = "linux") {
        Some(root.join("xdg/systemd/user/gobby-daemon.service"))
    } else if cfg!(target_os = "windows") {
        Some(root.join("gobby/gobby-daemon.task.xml"))
    } else {
        None
    }
}

fn write_registration(path: &Path) {
    std::fs::create_dir_all(path.parent().expect("registration parent"))
        .expect("create registration parent");
    std::fs::write(path, "registered").expect("write registration");
}
