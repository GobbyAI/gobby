use super::positive_usize;
use clap::{ArgGroup, Args, FromArgMatches, ValueEnum};

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
pub(crate) enum GraphViewKind {
    Fcg,
    Mcg,
    #[value(name = "class-hierarchy")]
    ClassHierarchy,
}

impl GraphViewKind {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Fcg => "fcg",
            Self::Mcg => "mcg",
            Self::ClassHierarchy => "class-hierarchy",
        }
    }

    pub(crate) fn default_depth(self) -> u32 {
        match self {
            Self::ClassHierarchy => 8,
            Self::Fcg | Self::Mcg => 1,
        }
    }

    pub(crate) fn effective_depth(self, depth: Option<u32>) -> u32 {
        depth.unwrap_or_else(|| self.default_depth())
    }

    pub(crate) fn allows_row_limits(self) -> bool {
        matches!(self, Self::Fcg | Self::Mcg)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum GraphViewSeed {
    File(String),
    Module(String),
    Symbol(String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct GraphViewArgs {
    pub view: GraphViewKind,
    pub seed: GraphViewSeed,
    pub depth: Option<u32>,
    pub incoming_limit: Option<usize>,
    pub outgoing_limit: Option<usize>,
}

impl GraphViewArgs {
    pub(crate) fn effective_depth(&self) -> u32 {
        self.view.effective_depth(self.depth)
    }
}

#[derive(Args, Clone, Debug)]
#[command(group(
    ArgGroup::new("seed")
        .required(true)
        .multiple(false)
        .args(["file", "module", "symbol"])
))]
struct GraphViewArgsRaw {
    /// View kind: fcg, mcg, or class-hierarchy
    #[arg(long, value_enum)]
    view: GraphViewKind,
    /// Project file seed (mcg only)
    #[arg(long, value_name = "FILE")]
    file: Option<String>,
    /// Module seed (mcg only)
    #[arg(long, value_name = "MODULE")]
    module: Option<String>,
    /// Symbol query seed (fcg and class-hierarchy only)
    #[arg(long, value_name = "SYMBOL")]
    symbol: Option<String>,
    /// Hop depth (1..=16). Omitted: 8 for class-hierarchy, 1 for fcg/mcg
    #[arg(
        long,
        value_parser = clap::value_parser!(u32)
            .range(1..=crate::graph::code_graph::MAX_SYMBOL_PATH_DEPTH as i64)
    )]
    depth: Option<u32>,
    /// Incoming neighbor limit (fcg and mcg only)
    #[arg(long, value_parser = positive_usize)]
    incoming_limit: Option<usize>,
    /// Outgoing neighbor limit (fcg and mcg only)
    #[arg(long, value_parser = positive_usize)]
    outgoing_limit: Option<usize>,
}

impl FromArgMatches for GraphViewArgs {
    fn from_arg_matches(matches: &clap::ArgMatches) -> Result<Self, clap::Error> {
        Self::from_arg_matches_mut(&mut matches.clone())
    }

    fn from_arg_matches_mut(matches: &mut clap::ArgMatches) -> Result<Self, clap::Error> {
        let raw = GraphViewArgsRaw::from_arg_matches_mut(matches)?;
        if !raw.view.allows_row_limits()
            && (raw.incoming_limit.is_some() || raw.outgoing_limit.is_some())
        {
            return Err(clap::Error::raw(
                clap::error::ErrorKind::ArgumentConflict,
                "--incoming-limit and --outgoing-limit cannot be used with --view=class-hierarchy",
            ));
        }
        let seed = match (raw.file, raw.module, raw.symbol) {
            (Some(file), None, None) => GraphViewSeed::File(file),
            (None, Some(module), None) => GraphViewSeed::Module(module),
            (None, None, Some(symbol)) => GraphViewSeed::Symbol(symbol),
            _ => {
                return Err(clap::Error::raw(
                    clap::error::ErrorKind::MissingRequiredArgument,
                    "exactly one of --file, --module, or --symbol is required",
                ));
            }
        };
        let selector_is_valid = matches!(
            (raw.view, &seed),
            (
                GraphViewKind::Mcg,
                GraphViewSeed::File(_) | GraphViewSeed::Module(_)
            ) | (
                GraphViewKind::Fcg | GraphViewKind::ClassHierarchy,
                GraphViewSeed::Symbol(_)
            )
        );
        if !selector_is_valid {
            return Err(clap::Error::raw(
                clap::error::ErrorKind::ArgumentConflict,
                match raw.view {
                    GraphViewKind::Mcg => "--view=mcg requires --file or --module",
                    GraphViewKind::Fcg | GraphViewKind::ClassHierarchy => {
                        "--view=fcg and --view=class-hierarchy require --symbol"
                    }
                },
            ));
        }
        Ok(Self {
            view: raw.view,
            seed,
            depth: raw.depth,
            incoming_limit: raw.incoming_limit,
            outgoing_limit: raw.outgoing_limit,
        })
    }

    fn update_from_arg_matches(&mut self, matches: &clap::ArgMatches) -> Result<(), clap::Error> {
        self.update_from_arg_matches_mut(&mut matches.clone())
    }

    fn update_from_arg_matches_mut(
        &mut self,
        matches: &mut clap::ArgMatches,
    ) -> Result<(), clap::Error> {
        *self = Self::from_arg_matches_mut(matches)?;
        Ok(())
    }
}

impl Args for GraphViewArgs {
    fn group_id() -> Option<clap::Id> {
        GraphViewArgsRaw::group_id()
    }

    fn augment_args(cmd: clap::Command) -> clap::Command {
        GraphViewArgsRaw::augment_args(cmd)
    }

    fn augment_args_for_update(cmd: clap::Command) -> clap::Command {
        GraphViewArgsRaw::augment_args_for_update(cmd)
    }
}
