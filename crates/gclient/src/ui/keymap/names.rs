// upstream: herdr v0.8.0 src/config/keybinds.rs
//! The keymap's tables: the `Action` enum, its table names, and herdr's
//! v0.8.0 default chords for the keep-set plus Gobby's control bindings.
//! `keymap.rs` builds the `Keymap` over these.

/// Dispatchable actions. herdr's keep-set names are kept verbatim except that
/// its `workspace` noun becomes Gobby's `project` (a sidebar card) where the
/// action walks the sidebar and `terminal` where it acts on a pane, and its
/// `agent` noun becomes `attention`. `CustomCommand` is reserved for the
/// plugin-menu decision (#20201): present in the table, never dispatched.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Action {
    Help,
    Settings,
    NewTerminal,
    NewProject,
    RenameTerminal,
    CloseTerminal,
    TerminalPicker,
    Goto,
    NavigateUp,
    NavigateDown,
    NavigatePaneLeft,
    NavigatePaneDown,
    NavigatePaneUp,
    NavigatePaneRight,
    Detach,
    ReloadConfig,
    OpenNotificationTarget,
    PreviousTerminal,
    NextTerminal,
    PreviousProject,
    NextProject,
    ToggleGroup,
    CycleMachineFilter,
    ToggleAgentSort,
    PreviousAttention,
    NextAttention,
    FocusAttention(u8),
    NewTab,
    RenameTab,
    PreviousTab,
    NextTab,
    SwitchTab(u8),
    SwitchProject(u8),
    CloseTab,
    RenamePane,
    CopyMode,
    FocusPaneLeft,
    FocusPaneDown,
    FocusPaneUp,
    FocusPaneRight,
    SwapPaneLeft,
    SwapPaneDown,
    SwapPaneUp,
    SwapPaneRight,
    CyclePaneNext,
    CyclePanePrevious,
    LastPane,
    SplitVertical,
    SplitHorizontal,
    ClosePane,
    Zoom,
    ResizeMode,
    ToggleSidebar,
    TakeControl,
    ReleaseControl,
    TakeBack,
    Respond,
    Quit,
    CustomCommand,
}

/// One row of the binding table: an action name, its herdr default chords,
/// and whether it is reserved (non-dispatchable, hidden from help).
#[derive(Debug, Clone, Copy)]
pub struct BindingSpec {
    pub name: &'static str,
    pub description: &'static str,
    pub defaults: &'static [&'static str],
    pub reserved: bool,
    pub indexed: bool,
}

const fn spec(
    name: &'static str,
    description: &'static str,
    defaults: &'static [&'static str],
) -> BindingSpec {
    BindingSpec {
        name,
        description,
        defaults,
        reserved: false,
        indexed: false,
    }
}

const fn indexed(
    name: &'static str,
    description: &'static str,
    defaults: &'static [&'static str],
) -> BindingSpec {
    BindingSpec {
        name,
        description,
        defaults,
        reserved: false,
        indexed: true,
    }
}

/// herdr v0.8.0 `KeysConfig::default()` for the keep-set, plus Gobby's
/// control bindings. Repository-checkout and mobile bindings are absent by design.
pub const BINDINGS: &[BindingSpec] = &[
    spec("help", "Open keybinding help", &["prefix+?"]),
    spec("settings", "Open settings", &["prefix+s"]),
    spec("new_terminal", "Open a new terminal", &[]),
    spec("new_project", "Add a project", &["prefix+shift+n"]),
    spec(
        "rename_terminal",
        "Rename the selected terminal",
        &["prefix+shift+w"],
    ),
    spec(
        "close_terminal",
        "Close the selected terminal",
        &["prefix+shift+d"],
    ),
    spec("terminal_picker", "Open the terminal picker", &["prefix+w"]),
    spec("goto", "Open the navigator", &["prefix+g"]),
    spec("navigate_up", "Select the previous sidebar row", &["up"]),
    spec("navigate_down", "Select the next sidebar row", &["down"]),
    spec("navigate_pane_left", "Focus the pane to the left", &["h"]),
    spec("navigate_pane_down", "Focus the pane below", &["j"]),
    spec("navigate_pane_up", "Focus the pane above", &["k"]),
    spec("navigate_pane_right", "Focus the pane to the right", &["l"]),
    spec("detach", "Release control", &["prefix+q"]),
    spec(
        "reload_config",
        "Reload client preferences",
        &["prefix+shift+r"],
    ),
    spec(
        "open_notification_target",
        "Focus the notification target",
        &["prefix+o"],
    ),
    spec("previous_terminal", "Select the previous terminal", &[]),
    spec("next_terminal", "Select the next terminal", &[]),
    spec("previous_project", "Focus the previous project", &[]),
    spec("next_project", "Focus the next project", &[]),
    spec(
        "toggle_group",
        "Collapse or expand the project's worktrees",
        &[],
    ),
    spec(
        "cycle_machine_filter",
        "Cycle the agents section's machine filter",
        &[],
    ),
    spec(
        "toggle_agent_sort",
        "Toggle grouped/priority agent order",
        &[],
    ),
    spec(
        "previous_attention",
        "Focus the previous attention prompt",
        &[],
    ),
    spec("next_attention", "Focus the next attention prompt", &[]),
    indexed("focus_attention", "Focus attention prompt 1-9", &[]),
    spec("new_tab", "Open a new tab", &["prefix+c"]),
    spec("rename_tab", "Rename the active tab", &["prefix+shift+t"]),
    spec("previous_tab", "Select the previous tab", &["prefix+p"]),
    spec("next_tab", "Select the next tab", &["prefix+n"]),
    indexed("switch_tab", "Switch to tab 1-9", &["prefix+1..9"]),
    indexed("switch_project", "Focus project 1-9", &[]),
    spec("close_tab", "Close the active tab", &["prefix+shift+x"]),
    spec(
        "rename_pane",
        "Rename the focused pane",
        &["prefix+shift+p"],
    ),
    spec("copy_mode", "Enter copy mode", &["prefix+["]),
    spec(
        "focus_pane_left",
        "Focus the pane to the left",
        &["prefix+h"],
    ),
    spec("focus_pane_down", "Focus the pane below", &["prefix+j"]),
    spec("focus_pane_up", "Focus the pane above", &["prefix+k"]),
    spec(
        "focus_pane_right",
        "Focus the pane to the right",
        &["prefix+l"],
    ),
    spec(
        "swap_pane_left",
        "Swap with the pane to the left",
        &["prefix+shift+h"],
    ),
    spec(
        "swap_pane_down",
        "Swap with the pane below",
        &["prefix+shift+j"],
    ),
    spec(
        "swap_pane_up",
        "Swap with the pane above",
        &["prefix+shift+k"],
    ),
    spec(
        "swap_pane_right",
        "Swap with the pane to the right",
        &["prefix+shift+l"],
    ),
    spec("cycle_pane_next", "Cycle to the next pane", &["prefix+tab"]),
    spec(
        "cycle_pane_previous",
        "Cycle to the previous pane",
        &["prefix+shift+tab"],
    ),
    spec("last_pane", "Focus the last focused pane", &[]),
    spec("split_vertical", "Split side by side", &["prefix+v"]),
    spec("split_horizontal", "Split stacked", &["prefix+minus"]),
    spec("close_pane", "Close the focused pane", &["prefix+x"]),
    spec("zoom", "Toggle zoom for the focused pane", &["prefix+z"]),
    spec("resize_mode", "Enter resize mode", &["prefix+r"]),
    spec("toggle_sidebar", "Toggle the sidebar", &["prefix+b"]),
    spec(
        "take_control",
        "Take control of the focused terminal",
        &["prefix+t"],
    ),
    spec(
        "release_control",
        "Release control of the focused terminal",
        &["prefix+u"],
    ),
    spec(
        "take_back",
        "Accept the take-back prompt",
        &["prefix+shift+a"],
    ),
    spec("respond", "Answer the attention prompt", &["prefix+a"]),
    spec("quit", "Quit the client", &["prefix+shift+q"]),
    // Reserved until the plugin/command-menu decision (#20201): the chord is
    // held in the table so no override can silently collide with it, but it
    // never dispatches and never appears in help.
    BindingSpec {
        name: "custom_command",
        description: "Run a custom command (reserved)",
        defaults: &["prefix+m"],
        reserved: true,
        indexed: false,
    },
];

/// Table names for every non-indexed action; `name` and `from_name` are
/// generated from one list so the compiler checks both for exhaustiveness.
macro_rules! action_names {
    ($($name:literal => $variant:ident),* $(,)?) => {
        impl Action {
            /// Table name for this action (`switch_tab` for `SwitchTab(3)`).
            pub fn name(self) -> &'static str {
                match self {
                    Action::FocusAttention(_) => "focus_attention",
                    Action::SwitchTab(_) => "switch_tab",
                    Action::SwitchProject(_) => "switch_project",
                    $(Action::$variant => $name,)*
                }
            }

            /// Resolve a table name, with `index` (1-9) for indexed actions.
            pub fn from_name(name: &str, index: Option<u8>) -> Option<Action> {
                let index = index.filter(|i| (1..=9).contains(i));
                match name {
                    "focus_attention" => index.map(Action::FocusAttention),
                    "switch_tab" => index.map(Action::SwitchTab),
                    "switch_project" => index.map(Action::SwitchProject),
                    $($name => Some(Action::$variant),)*
                    _ => None,
                }
            }
        }
    };
}

action_names! {
    "help" => Help, "settings" => Settings, "new_terminal" => NewTerminal,
    "new_project" => NewProject, "rename_terminal" => RenameTerminal, "close_terminal" => CloseTerminal,
    "terminal_picker" => TerminalPicker, "goto" => Goto, "navigate_up" => NavigateUp,
    "navigate_down" => NavigateDown, "navigate_pane_left" => NavigatePaneLeft,
    "navigate_pane_down" => NavigatePaneDown, "navigate_pane_up" => NavigatePaneUp,
    "navigate_pane_right" => NavigatePaneRight, "detach" => Detach, "reload_config" => ReloadConfig,
    "open_notification_target" => OpenNotificationTarget, "previous_terminal" => PreviousTerminal,
    "next_terminal" => NextTerminal, "previous_project" => PreviousProject,
    "next_project" => NextProject, "toggle_group" => ToggleGroup,
    "cycle_machine_filter" => CycleMachineFilter, "toggle_agent_sort" => ToggleAgentSort,
    "previous_attention" => PreviousAttention,
    "next_attention" => NextAttention, "new_tab" => NewTab, "rename_tab" => RenameTab,
    "previous_tab" => PreviousTab, "next_tab" => NextTab, "close_tab" => CloseTab,
    "rename_pane" => RenamePane, "copy_mode" => CopyMode,
    "focus_pane_left" => FocusPaneLeft, "focus_pane_down" => FocusPaneDown,
    "focus_pane_up" => FocusPaneUp, "focus_pane_right" => FocusPaneRight,
    "swap_pane_left" => SwapPaneLeft, "swap_pane_down" => SwapPaneDown,
    "swap_pane_up" => SwapPaneUp, "swap_pane_right" => SwapPaneRight,
    "cycle_pane_next" => CyclePaneNext, "cycle_pane_previous" => CyclePanePrevious,
    "last_pane" => LastPane, "split_vertical" => SplitVertical,
    "split_horizontal" => SplitHorizontal, "close_pane" => ClosePane, "zoom" => Zoom,
    "resize_mode" => ResizeMode, "toggle_sidebar" => ToggleSidebar, "take_control" => TakeControl,
    "release_control" => ReleaseControl, "take_back" => TakeBack, "respond" => Respond,
    "quit" => Quit, "custom_command" => CustomCommand,
}

impl Action {
    pub fn is_reserved(self) -> bool {
        matches!(self, Action::CustomCommand)
    }
}
