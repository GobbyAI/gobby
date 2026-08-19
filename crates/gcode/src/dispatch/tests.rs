use super::*;
use crate::cli::Cli;
use crate::cli_error::CliError;
use clap::Parser;
use gobby_core::grant::GrantError;

fn services_for(args: &[&str]) -> config::ServiceConfigSelection {
    let cli = Cli::try_parse_from(std::iter::once("gcode").chain(args.iter().copied()))
        .expect("command parses");
    service_config_selection(&cli.command)
}

#[test]
fn stderr_logger_defaults_to_warnings_for_non_quiet_runs() {
    assert_eq!(stderr_log_level(false, None), log::LevelFilter::Warn);
}

#[test]
fn stderr_logger_respects_plain_rust_log_level() {
    assert_eq!(
        stderr_log_level(false, Some("debug")),
        log::LevelFilter::Debug
    );
}

#[test]
fn stderr_logger_uses_quiet_as_hard_mute() {
    assert_eq!(stderr_log_level(true, Some("warn")), log::LevelFilter::Off);
}

#[test]
fn lookup_commands_skip_service_config_resolution() {
    for args in [
        &["grep", "-F", "needle"][..],
        &["tree"][..],
        &["symbol-at", "src/lib.rs:10"][..],
        &["search-content", "needle"][..],
        &["search-text", "needle"][..],
        &["search-symbol", "needle"][..],
    ] {
        assert_eq!(
            services_for(args),
            config::ServiceConfigSelection::database_only()
        );
    }
}

#[test]
fn graph_and_vector_commands_request_only_needed_services() {
    assert_eq!(
        services_for(&["search", "concept"]),
        config::ServiceConfigSelection::hybrid_search()
    );
    assert_eq!(
        services_for(&["search-symbol", "needle", "--with-graph"]),
        config::ServiceConfigSelection::falkordb_only()
    );
    assert_eq!(
        services_for(&["callers", "needle"]),
        config::ServiceConfigSelection::falkordb_only()
    );
    assert_eq!(
        services_for(&["vector", "cleanup-orphans"]),
        config::ServiceConfigSelection::qdrant_only()
    );
    assert_eq!(
        services_for(&["prune", "--force"]),
        config::ServiceConfigSelection::projection_cleanup()
    );
    assert_eq!(
        services_for(&["embeddings", "doctor"]),
        config::ServiceConfigSelection::vectors()
    );
}

#[test]
fn invalidate_requests_projection_cleanup_services() {
    assert_eq!(
        services_for(&[
            "invalidate",
            "--project-id",
            "019bfef8-89bb-7bd1-a5c3-80baabdff01b",
            "--force",
        ]),
        config::ServiceConfigSelection::projection_cleanup()
    );
}

#[test]
fn degraded_freshness_warning_names_allow_stale() {
    let line = freshness_warning(
        false,
        &freshness::FreshnessStatus::Degraded("disk full".to_string()),
    )
    .expect("degraded status should warn");
    assert_eq!(
        line,
        "warning: index refresh failed (disk full); serving existing index \
         (pass --allow-stale to skip this check)"
    );
    assert!(!line.contains('\n'));
}

#[test]
fn degraded_freshness_warning_is_suppressed_when_quiet() {
    assert_eq!(
        freshness_warning(
            true,
            &freshness::FreshnessStatus::Degraded("disk full".to_string()),
        ),
        None
    );
}

#[test]
fn cli_error_uses_its_exit_status() {
    let error = anyhow::Error::from(CliError {
        code: "usage",
        message: "unknown argument".to_string(),
        recovery: None,
        exit_status: 2,
    });
    assert_eq!(classify_run_error(&error), 2);

    let doctor = anyhow::Error::from(CliError {
        code: "embeddings_doctor",
        message: "config drift".to_string(),
        recovery: None,
        exit_status: 11,
    });
    assert_eq!(classify_run_error(&doctor), 11);
}

#[test]
fn grant_error_maps_through_cli_error_grant() {
    let error = anyhow::Error::from(GrantError::DaemonRequired);
    assert_eq!(
        classify_run_error(&error),
        CliError::grant(GrantError::DaemonRequired).exit_status
    );
    assert_eq!(classify_run_error(&error), 2);
}

#[test]
fn unclassified_anyhow_error_exits_one() {
    let error = anyhow::anyhow!("unexpected index panic");
    assert_eq!(classify_run_error(&error), 1);
}
