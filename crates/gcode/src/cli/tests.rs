use super::*;
use clap::{CommandFactory, Parser};
use std::collections::BTreeSet;

mod grep;
mod projection;
mod search;
mod setup;
mod top_level;

#[test]
fn clap_command_leaves_are_documented_in_contract() {
    let command = Cli::command();
    let clap_commands = clap_command_leaves(&command);
    let contract_commands = gobby_code::contract::contract()
        .commands
        .into_iter()
        .map(|command| command.name.to_owned())
        .collect::<BTreeSet<_>>();
    let missing = clap_commands
        .difference(&contract_commands)
        .cloned()
        .collect::<Vec<_>>();

    assert!(
        missing.is_empty(),
        "missing contract entries for clap commands: {}",
        missing.join(", ")
    );
}

#[test]
fn prune_help_describes_stale_project_removal_and_content_gc() {
    let command = Cli::command();
    let prune = command
        .find_subcommand("prune")
        .expect("prune subcommand is registered");
    let about = prune
        .get_about()
        .map(ToString::to_string)
        .unwrap_or_default();

    assert!(
        about.contains("stale projects") && about.contains("content versions"),
        "prune help must describe stale-project removal and content-version GC: {about}"
    );
    assert!(
        !about.contains("orphaned"),
        "prune help must not describe orphan projection reconciliation: {about}"
    );
}

fn clap_command_leaves(command: &clap::Command) -> BTreeSet<String> {
    let mut leaves = BTreeSet::new();
    collect_clap_command_leaves(command, None, &mut leaves);
    leaves
}

fn collect_clap_command_leaves(
    command: &clap::Command,
    prefix: Option<&str>,
    leaves: &mut BTreeSet<String>,
) {
    for subcommand in command.get_subcommands() {
        let path = match prefix {
            Some(prefix) => format!("{prefix} {}", subcommand.get_name()),
            None => subcommand.get_name().to_owned(),
        };

        if subcommand.get_subcommands().next().is_some() {
            collect_clap_command_leaves(subcommand, Some(&path), leaves);
        } else {
            leaves.insert(path);
        }
    }
}
