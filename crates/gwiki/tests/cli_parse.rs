use std::fs;
use std::path::Path;
use std::process::{Command, Output};

mod common;

fn gwiki(args: &[&str]) -> Output {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let project = tmp.path().join("project");
    common::write_gcode_json(&project);
    fs::write(project.join("README.md"), "# Parse fixture\n").expect("write ingest fixture");
    if args.first().is_none_or(|command| *command != "init") {
        if args.contains(&"--topic") {
            let mut command = common::gwiki_command();
            strip_db_env(&mut command)
                .args(["init", "--topic", "rust"])
                .current_dir(&project);
            apply_isolated_env(&mut command, tmp.path(), &hub);
            let output = command.output().expect("gwiki topic init runs");
            assert!(
                output.status.success(),
                "topic init fixture failed\nstdout:\n{}\nstderr:\n{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
        } else if args.contains(&"--project") {
            let mut command = common::gwiki_command();
            strip_db_env(&mut command)
                .args(["init", "--project"])
                .current_dir(&project);
            apply_isolated_env(&mut command, tmp.path(), &hub);
            let output = command.output().expect("gwiki project init runs");
            assert!(
                output.status.success(),
                "project init fixture failed\nstdout:\n{}\nstderr:\n{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }

    let mut command = common::gwiki_command();
    strip_db_env(&mut command).args(args).current_dir(&project);
    apply_isolated_env(&mut command, tmp.path(), &hub);
    command.output().expect("gwiki binary runs")
}

fn strip_db_env(command: &mut Command) -> &mut Command {
    command
        .env_remove("GWIKI_DATABASE_URL")
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .env_remove("GWIKI_POSTGRES_TEST_DATABASE_URL")
        .env_remove("GCODE_POSTGRES_TEST_DATABASE_URL")
}

fn apply_isolated_env<'a>(command: &'a mut Command, root: &Path, hub: &Path) -> &'a mut Command {
    command
        .env("GOBBY_WIKI_HUB", hub)
        .env("HOME", root.join("home"))
        .env("XDG_CONFIG_HOME", root.join("xdg-config"))
        .env("XDG_DATA_HOME", root.join("xdg-data"))
        .env("XDG_CACHE_HOME", root.join("xdg-cache"))
        .env("XDG_STATE_HOME", root.join("xdg-state"))
}

#[test]
fn core_commands_parse_scope_flags() {
    let cases = [
        vec!["init", "--topic", "rust"],
        vec!["setup", "--topic", "rust"],
        vec!["index", "--topic", "rust"],
        vec!["ingest-file", "--topic", "rust", "README.md"],
        vec!["search", "--topic", "rust", "ownership"],
        vec!["read", "--topic", "rust", "--path", "wiki/topics/rust.md"],
        vec!["read", "--topic", "rust", "--title", "Rust"],
        vec!["backlinks", "--topic", "rust", "wiki/topics/rust.md"],
        vec!["link-suggest", "--topic", "rust"],
        vec!["audit", "--topic", "rust"],
        vec!["lint", "--topic", "rust"],
        vec!["health", "--topic", "rust"],
        vec!["status", "--topic", "rust"],
        vec!["--project", "search", "ownership"],
    ];

    for args in cases {
        let output = gwiki(&args);
        assert!(
            output.status.success(),
            "{args:?} failed\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    let quiet = gwiki(&["--quiet", "status", "--topic", "rust"]);
    assert!(quiet.status.success());
    assert_eq!(String::from_utf8_lossy(&quiet.stderr), "");
    assert!(String::from_utf8_lossy(&quiet.stdout).contains("\"status\": \"shell-ready\""));
}
