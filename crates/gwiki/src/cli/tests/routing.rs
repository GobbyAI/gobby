use clap::Parser;

use gobby_core::config::AiRouting;
use gobby_wiki::Command;

use super::*;

const ROUTING_FLAGS: &[&str] = &[
    "--ai",
    "--transcription-routing",
    "--vision-routing",
    "--text-routing",
    "--require-ai",
];

fn assert_unknown_argument(args: &[&str]) {
    let error = Cli::try_parse_from(args).expect_err(&format!("expected unknown: {args:?}"));
    assert_eq!(
        error.kind(),
        clap::error::ErrorKind::UnknownArgument,
        "{args:?}: {error}"
    );
}

#[test]
fn gwiki_rejects_routing_flags_except_no_ai() {
    assert_unknown_argument(&["gwiki", "code", "--ai", "off"]);
    assert_unknown_argument(&["gwiki", "compile", "--ai", "off"]);
    assert_unknown_argument(&["gwiki", "upkeep", "--ai", "off"]);
    assert_unknown_argument(&["gwiki", "recap", "--ai", "off"]);
    assert_unknown_argument(&["gwiki", "librarian", "--ai", "off"]);
    assert_unknown_argument(&[
        "gwiki",
        "ingest-file",
        "media/a.mp3",
        "--transcription-routing",
        "daemon",
    ]);
    assert_unknown_argument(&[
        "gwiki",
        "ingest-file",
        "media/a.mp3",
        "--vision-routing",
        "off",
    ]);
    assert_unknown_argument(&[
        "gwiki",
        "ingest-file",
        "media/a.mp3",
        "--text-routing",
        "daemon",
    ]);

    let mut command = Cli::command();
    let mut help = command.render_long_help().to_string();
    for sub in command.get_subcommands_mut() {
        help.push_str(&sub.render_long_help().to_string());
    }
    for flag in ROUTING_FLAGS {
        assert!(!contains_flag(&help, flag), "help still advertises {flag}");
    }
    assert!(contains_flag(&help, "--no-ai"), "help must keep --no-ai");
}

fn contains_flag(text: &str, flag: &str) -> bool {
    text.split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '-'))
        .any(|token| token == flag)
}

#[test]
fn no_ai_maps_commands_to_off() {
    let upkeep = Cli::try_parse_from(["gwiki", "upkeep", "--no-ai"]).expect("upkeep --no-ai");
    let command = command_from_cli(upkeep.command, upkeep.scope.into()).expect("map upkeep");
    let Command::Upkeep { ai, .. } = command else {
        panic!("expected upkeep");
    };
    assert_eq!(ai, AiRouting::Off);

    let recap = Cli::try_parse_from(["gwiki", "recap", "--no-ai"]).expect("recap --no-ai");
    let command = command_from_cli(recap.command, recap.scope.into()).expect("map recap");
    let Command::Recap { ai, .. } = command else {
        panic!("expected recap");
    };
    assert_eq!(ai, AiRouting::Off);

    let compile = Cli::try_parse_from(["gwiki", "compile", "--no-ai"]).expect("compile --no-ai");
    let command = command_from_cli(compile.command, compile.scope.into()).expect("map compile");
    let Command::Compile { ai, .. } = command else {
        panic!("expected compile");
    };
    assert_eq!(ai, AiRouting::Off);

    let librarian =
        Cli::try_parse_from(["gwiki", "librarian", "--no-ai"]).expect("librarian --no-ai");
    let command =
        command_from_cli(librarian.command, librarian.scope.into()).expect("map librarian");
    let Command::Librarian { ai, .. } = command else {
        panic!("expected librarian");
    };
    assert_eq!(ai, AiRouting::Off);

    let ingest = Cli::try_parse_from(["gwiki", "ingest-file", "media/a.mp3", "--no-ai"])
        .expect("ingest --no-ai");
    let command = command_from_cli(ingest.command, ingest.scope.into()).expect("map ingest");
    let Command::IngestFile { options, .. } = command else {
        panic!("expected ingest-file");
    };
    assert!(options.no_ai);
}

#[test]
fn code_no_ai_sets_off_override() {
    let cli = Cli::try_parse_from(["gwiki", "code", "--project", "/repo", "--no-ai"])
        .expect("code --no-ai");
    let command = command_from_cli(cli.command, cli.scope.into()).expect("map code");
    let Command::Code(options) = command else {
        panic!("expected code");
    };
    assert_eq!(options.ai, Some(AiRouting::Off));
}
