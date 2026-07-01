use super::*;
use clap::{CommandFactory, Parser};
use std::collections::BTreeSet;

mod codewiki;
mod grep;
mod projection;
mod search;
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
