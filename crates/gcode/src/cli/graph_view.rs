use super::positive_usize;
use clap::{ArgGroup, Args, FromArgMatches, ValueEnum};

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
pub(crate) enum GraphViewKind {
    Fcg,
    Mcg,
    #[value(name = "class-hierarchy")]
    ClassHierarchy,
    Communities,
}

impl GraphViewKind {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Fcg => "fcg",
            Self::Mcg => "mcg",
            Self::ClassHierarchy => "class-hierarchy",
            Self::Communities => "communities",
        }
    }

    pub(crate) fn default_depth(self) -> u32 {
        match self {
            Self::ClassHierarchy => 8,
            Self::Communities => 0,
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
    Community(String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct GraphViewArgs {
    pub view: GraphViewKind,
    pub seed: Option<GraphViewSeed>,
    pub depth: Option<u32>,
    pub incoming_limit: Option<usize>,
    pub outgoing_limit: Option<usize>,
    pub min_size: Option<usize>,
}

impl GraphViewArgs {
    pub(crate) fn effective_depth(&self) -> u32 {
        self.view.effective_depth(self.depth)
    }

    pub(crate) fn effective_min_size(&self) -> usize {
        self.min_size.unwrap_or(2)
    }
}

#[derive(Args, Clone, Debug)]
#[command(group(
    ArgGroup::new("seed")
        .required(false)
        .multiple(false)
        .args(["file", "module", "symbol", "community"])
))]
struct GraphViewArgsRaw {
    /// View kind: fcg, mcg, class-hierarchy, or communities
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
    /// Stored community selector (communities only)
    #[arg(long, value_name = "ID|LABEL|PATH")]
    community: Option<String>,
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
    /// Minimum listed community size (communities only)
    #[arg(long, value_parser = positive_usize)]
    min_size: Option<usize>,
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
                format!(
                    "--incoming-limit and --outgoing-limit cannot be used with --view={}",
                    raw.view.as_str()
                ),
            ));
        }
        if raw.view == GraphViewKind::Communities && raw.depth.is_some() {
            return Err(clap::Error::raw(
                clap::error::ErrorKind::ArgumentConflict,
                "--depth cannot be used with --view=communities",
            ));
        }
        if raw.view != GraphViewKind::Communities && raw.min_size.is_some() {
            return Err(clap::Error::raw(
                clap::error::ErrorKind::ArgumentConflict,
                "--min-size can only be used with --view=communities",
            ));
        }
        let seed = match (raw.file, raw.module, raw.symbol, raw.community) {
            (Some(file), None, None, None) => Some(GraphViewSeed::File(file)),
            (None, Some(module), None, None) => Some(GraphViewSeed::Module(module)),
            (None, None, Some(symbol), None) => Some(GraphViewSeed::Symbol(symbol)),
            (None, None, None, Some(community)) => Some(GraphViewSeed::Community(community)),
            (None, None, None, None) => None,
            _ => None,
        };
        let selector_is_valid = matches!(
            (raw.view, &seed),
            (
                GraphViewKind::Mcg,
                Some(GraphViewSeed::File(_) | GraphViewSeed::Module(_))
            ) | (
                GraphViewKind::Fcg | GraphViewKind::ClassHierarchy,
                Some(GraphViewSeed::Symbol(_))
            ) | (
                GraphViewKind::Communities,
                None | Some(GraphViewSeed::Community(_))
            )
        );
        if !selector_is_valid {
            let missing = seed.is_none() && raw.view != GraphViewKind::Communities;
            return Err(clap::Error::raw(
                if missing {
                    clap::error::ErrorKind::MissingRequiredArgument
                } else {
                    clap::error::ErrorKind::ArgumentConflict
                },
                match raw.view {
                    GraphViewKind::Mcg => "--view=mcg requires --file or --module",
                    GraphViewKind::Fcg | GraphViewKind::ClassHierarchy => {
                        "--view=fcg and --view=class-hierarchy require --symbol"
                    }
                    GraphViewKind::Communities => {
                        "--view=communities accepts only --community or no seed"
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
            min_size: raw.min_size,
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
