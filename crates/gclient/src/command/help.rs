//! The verb table behind `gclient help`, `gclient help VERB` and
//! `gclient VERB --help`.

/// One verb: its name, usage line, summary, and any detail its own help adds.
struct Verb {
    name: &'static str,
    usage: &'static str,
    summary: &'static str,
    detail: &'static str,
}

const VERBS: &[Verb] = &[
    Verb {
        name: "list",
        usage: "list [--workspace REF]",
        summary: "List tabs and panes",
        detail: "Prints each tab's id and title, then its panes' id, label and terminal.",
    },
    Verb {
        name: "new-tab",
        usage: "new-tab --project NAME|ID [--workspace REF] [--name TITLE]",
        summary: "Create a tab",
        detail: "Prints the new tab's REF and its first pane's REF.",
    },
    Verb {
        name: "split",
        usage: "split [REF] --right|--down [--cmd TEXT]",
        summary: "Split a pane",
        detail: "--cmd types TEXT into the new pane and presses Enter.",
    },
    Verb {
        name: "resize",
        usage: "resize [REF] RATIO",
        summary: "Resize a split",
        detail: "RATIO is the pane's share of its split, above 0 and below 1.",
    },
    Verb {
        name: "title",
        usage: "title [REF] TEXT [--kind tab|pane]",
        summary: "Rename a tab or pane",
        detail: "A node:workspace:tab REF names a tab; pass --kind tab for a tab UUID.",
    },
    Verb {
        name: "select",
        usage: "select [REF] [--workspace REF] [--tab-ref TAB]",
        summary: "Show a tab or pane in every running window",
        detail: "select stores REF as the workspace's focus and switches every running gclient \
                 window\non that workspace to its tab and pane; a tab REF keeps that tab's own \
                 focused pane.\nselect derives workspace from a full REF, otherwise from \
                 --workspace or GOBBY_WORKSPACE_ID.\nA pane UUID needs its tab: --tab-ref TAB or \
                 GOBBY_TAB_ID.",
    },
    Verb {
        name: "send-keys",
        usage: "send-keys [REF] TEXT [--enter]",
        summary: "Send pane text",
        detail: "Types TEXT into the pane; --enter presses Enter after it.\n\
                 With --enter the daemon reads the composer back before it answers, \
                 which can take tens of seconds. A command_not_submitted error means \
                 the CLI kept the text; other errors do not confirm submission.\n\
                 Put TEXT after -- when it starts with a dash.\n\
                 On a timeout the text may already be in the pane: check it with \
                 capture-pane before retrying.",
    },
    Verb {
        name: "capture-pane",
        usage: "capture-pane [REF] [--lines N]",
        summary: "Read pane text",
        detail: "--lines N reads the last N lines.",
    },
    Verb {
        name: "wait-for-output",
        usage: "wait-for-output [REF] --pattern REGEX [--timeout S] [--interval S]",
        summary: "Wait for pane text",
        detail: "Exits 0 on a match, 1 on timeout (default 30 s), 2 when the pane is gone.",
    },
    Verb {
        name: "kill",
        usage: "kill [REF] [--kind tab|pane]",
        summary: "Close a tab or pane",
        detail: "A node:workspace:tab REF names a tab; pass --kind tab for a tab UUID.",
    },
    Verb {
        name: "help",
        usage: "help [VERB]",
        summary: "Show this table, or one verb's help",
        detail: "",
    },
];

const COMMON: &str = "\
Every verb takes --workspace REF, --json, --daemon-url URL and --token-file PATH.
REF is node:workspace:tab[:pane] (0:1:2:1), a tab or pane UUID, or a short UUID prefix.
--workspace selects the workspace and defaults to GOBBY_WORKSPACE_ID: list, new-tab and
select act on it, other verbs resolve a short ID in it, and an explicit --workspace
refuses a REF outside it.
Omitted pane REF uses GOBBY_PANE_REF; outside a pane, pass explicit values.
UUID tab refs: use --kind tab with title/kill; UUID pane focus: use --tab-ref TAB.
";

pub(super) fn is_verb(name: &str) -> bool {
    VERBS.iter().any(|verb| verb.name == name)
}

/// The whole table.
pub(super) fn table() -> String {
    let mut text = String::from("gclient <verb> [options]\n");
    for verb in VERBS {
        text.push_str(&format!("  {:<44} {}\n", verb.usage, verb.summary));
    }
    text.push_str(COMMON);
    text.push_str("gclient VERB --help shows one verb.\n");
    text
}

/// One verb's help, or `None` for an unknown name.
pub(super) fn verb(name: &str) -> Option<String> {
    let verb = VERBS.iter().find(|verb| verb.name == name)?;
    let mut text = format!("usage: gclient {}\n{}.\n", verb.usage, verb.summary);
    if !verb.detail.is_empty() {
        text.push_str(verb.detail);
        text.push('\n');
    }
    text.push('\n');
    text.push_str(COMMON);
    Some(text)
}
