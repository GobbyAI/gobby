use super::*;

#[test]
fn setup_is_not_a_command() {
    match Cli::try_parse_from(["gcode", "setup", "--standalone"]) {
        Ok(_) => panic!("gcode setup must no longer parse"),
        Err(error) => assert_eq!(error.kind(), clap::error::ErrorKind::InvalidSubcommand),
    }
}

#[test]
fn setup_long_help_is_absent() {
    match Cli::try_parse_from(["gcode", "setup", "--help"]) {
        Ok(_) => panic!("gcode setup --help must no longer parse"),
        Err(error) => assert_eq!(error.kind(), clap::error::ErrorKind::InvalidSubcommand),
    }
}
