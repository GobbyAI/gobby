//! 3.1.6 / 3.1.7: herdr defaults, client-local overrides, collisions.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use gobby_client::startup::{keymap_override_path, load_keymap, StartupError};
use gobby_client::ui::keymap::{
    default_override_path, default_prefix, Action, Keymap, KeymapError, Trigger, BINDINGS,
    HERDR_PREFIX,
};
use gobby_client::ui::settings::ClientPrefs;
use std::collections::HashSet;
use std::fs;

fn key(code: KeyCode, modifiers: KeyModifiers) -> KeyEvent {
    KeyEvent::new(code, modifiers)
}

fn ch(c: char) -> KeyEvent {
    key(KeyCode::Char(c), KeyModifiers::NONE)
}

fn shift(c: char) -> KeyEvent {
    key(KeyCode::Char(c), KeyModifiers::SHIFT)
}

fn chords_by_name(keymap: &Keymap) -> Vec<(&'static str, Vec<Trigger>)> {
    keymap
        .bindings
        .iter()
        .map(|b| (b.name, b.chords.iter().map(|c| c.trigger).collect()))
        .collect()
}

#[test]
fn overrides_preserve_defaults_and_cannot_activate_deferred_actions() {
    let defaults = Keymap::defaults(HERDR_PREFIX);

    // herdr v0.8.0 map, spot-checked against config/model.rs KeysConfig::default().
    assert!(defaults.is_prefix(&key(KeyCode::Char('b'), KeyModifiers::CONTROL)));
    assert!(!defaults.is_prefix(&key(KeyCode::Char('a'), KeyModifiers::CONTROL)));
    let prefix_map = [
        (ch('?'), Action::Help),
        (ch('s'), Action::Settings),
        (shift('N'), Action::NewProject),
        (shift('W'), Action::RenameTerminal),
        (shift('D'), Action::CloseTerminal),
        (ch('w'), Action::TerminalPicker),
        (ch('g'), Action::Goto),
        (ch('q'), Action::Detach),
        (shift('R'), Action::ReloadConfig),
        (ch('o'), Action::OpenNotificationTarget),
        (ch('c'), Action::NewTab),
        (shift('T'), Action::RenameTab),
        (ch('p'), Action::PreviousTab),
        (ch('n'), Action::NextTab),
        (ch('1'), Action::SwitchTab(1)),
        (ch('9'), Action::SwitchTab(9)),
        (shift('X'), Action::CloseTab),
        (shift('P'), Action::RenamePane),
        (ch('['), Action::CopyMode),
        (ch('h'), Action::FocusPaneLeft),
        (ch('j'), Action::FocusPaneDown),
        (ch('k'), Action::FocusPaneUp),
        (ch('l'), Action::FocusPaneRight),
        (shift('H'), Action::SwapPaneLeft),
        (shift('J'), Action::SwapPaneDown),
        (shift('K'), Action::SwapPaneUp),
        (shift('L'), Action::SwapPaneRight),
        (key(KeyCode::Tab, KeyModifiers::NONE), Action::CyclePaneNext),
        (
            key(KeyCode::BackTab, KeyModifiers::SHIFT),
            Action::CyclePanePrevious,
        ),
        (ch('v'), Action::SplitVertical),
        (ch('-'), Action::SplitHorizontal),
        (ch('x'), Action::ClosePane),
        (ch('z'), Action::Zoom),
        (ch('r'), Action::ResizeMode),
        (ch('b'), Action::ToggleSidebar),
        (ch('t'), Action::TakeControl),
        (ch('u'), Action::ReleaseControl),
        (shift('A'), Action::TakeBack),
        (ch('a'), Action::Respond),
        (shift('Q'), Action::Quit),
    ];
    // Direct (no prefix) chords: sidebar navigation reuses h/j/k/l, so those
    // events resolve differently with and without the prefix.
    let direct_map = [
        (key(KeyCode::Up, KeyModifiers::NONE), Action::NavigateUp),
        (key(KeyCode::Down, KeyModifiers::NONE), Action::NavigateDown),
        (ch('h'), Action::NavigatePaneLeft),
        (ch('j'), Action::NavigatePaneDown),
        (ch('k'), Action::NavigatePaneUp),
        (ch('l'), Action::NavigatePaneRight),
    ];
    for (event, action) in prefix_map {
        assert_eq!(defaults.lookup_prefix(&event), Some(action), "{event:?}");
        if !direct_map.iter().any(|(direct, _)| *direct == event) {
            assert_eq!(
                defaults.lookup_direct(&event),
                None,
                "{event:?} is prefix-only"
            );
        }
    }
    for (event, action) in direct_map {
        assert_eq!(defaults.lookup_direct(&event), Some(action), "{event:?}");
    }
    for name in [
        "new_terminal",
        "previous_terminal",
        "next_terminal",
        "previous_project",
        "next_project",
        "toggle_group",
        "previous_attention",
        "next_attention",
        "focus_attention",
        "switch_project",
        "last_pane",
    ] {
        assert!(
            defaults.binding(name).unwrap().chords.is_empty(),
            "{name} is unset"
        );
    }
    assert_eq!(defaults.binding("switch_tab").unwrap().chords.len(), 9);
    for spec in BINDINGS {
        let binding = defaults.binding(spec.name).unwrap();
        assert_eq!(binding.reserved, spec.reserved, "{}", spec.name);
    }
    assert_eq!(defaults.bindings.len(), BINDINGS.len());

    // Repository-checkout and mobile actions do not exist here.
    for name in [
        "new_worktree",
        "open_worktree",
        "remove_worktree",
        "mobile_menu",
    ] {
        assert!(defaults.binding(name).is_none(), "{name}");
        let err = Keymap::from_toml(
            &format!("[bindings]\n{name} = \"prefix+shift+g\"\n"),
            HERDR_PREFIX,
        )
        .unwrap_err();
        assert_eq!(err, KeymapError::UnknownAction(name.to_string()));
    }

    // A client-local override replaces exactly one action by name.
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("keymap.toml");
    fs::write(&path, "[bindings]\nhelp = \"prefix+f1\"\n").unwrap();
    let merged = Keymap::load_overrides(&path, HERDR_PREFIX).unwrap();
    assert_eq!(
        merged.lookup_prefix(&key(KeyCode::F(1), KeyModifiers::NONE)),
        Some(Action::Help)
    );
    assert_eq!(
        merged.lookup_prefix(&ch('?')),
        None,
        "replaced chord is released"
    );
    let before = chords_by_name(&defaults);
    let after = chords_by_name(&merged);
    for ((name, old), (_, new)) in before.iter().zip(after.iter()) {
        if *name == "help" {
            assert_ne!(old, new);
        } else {
            assert_eq!(old, new, "{name} changed by an unrelated override");
        }
    }
    assert!(merged.is_prefix(&key(KeyCode::Char('b'), KeyModifiers::CONTROL)));

    // Multiple chords, a prefix override, and a missing file.
    fs::write(
        &path,
        "prefix = \"ctrl+a\"\n[bindings]\nnext_tab = [\"prefix+n\", \"ctrl+alt+]\"]\n",
    )
    .unwrap();
    let merged = Keymap::load_overrides(&path, HERDR_PREFIX).unwrap();
    assert!(merged.is_prefix(&key(KeyCode::Char('a'), KeyModifiers::CONTROL)));
    assert!(!merged.is_prefix(&key(KeyCode::Char('b'), KeyModifiers::CONTROL)));
    assert_eq!(merged.lookup_prefix(&ch('n')), Some(Action::NextTab));
    assert_eq!(
        merged.lookup_direct(&key(
            KeyCode::Char(']'),
            KeyModifiers::CONTROL | KeyModifiers::ALT
        )),
        Some(Action::NextTab)
    );
    let missing = Keymap::load_overrides(&dir.path().join("absent.toml"), HERDR_PREFIX).unwrap();
    assert_eq!(chords_by_name(&missing), chords_by_name(&defaults));

    // Reserved actions stay non-dispatchable and hidden even when named.
    let err = Keymap::from_toml(
        "[bindings]\ncustom_command = \"prefix+shift+m\"\n",
        HERDR_PREFIX,
    )
    .unwrap_err();
    assert_eq!(
        err,
        KeymapError::ReservedAction("custom_command".to_string())
    );
    assert_eq!(
        defaults.lookup_prefix(&ch('m')),
        None,
        "reserved chord never dispatches"
    );
    let help = defaults.help_entries();
    assert!(help.iter().any(|e| e.name == "split_vertical"));
    assert!(help.iter().all(|e| e.name != "custom_command"));
    assert!(help.iter().all(|e| !e.keys.contains("prefix+m")));

    // Malformed input is a parse error, not a panic.
    assert!(matches!(
        Keymap::from_toml("[bindings\nhelp = 1", HERDR_PREFIX),
        Err(KeymapError::Parse(_))
    ));
    assert_eq!(
        Keymap::from_toml("[bindings]\nhelp = \"prefix+\"\n", HERDR_PREFIX).unwrap_err(),
        KeymapError::InvalidChord {
            action: "help".to_string(),
            chord: "prefix+".to_string()
        }
    );
}

#[test]
fn colliding_override_is_rejected_and_defaults_survive() {
    let err =
        Keymap::from_toml("[bindings]\nsplit_vertical = \"prefix+x\"\n", HERDR_PREFIX).unwrap_err();
    assert_eq!(
        err,
        KeymapError::Collision {
            chord: "prefix+x".to_string(),
            action: "split_vertical".to_string(),
            displaced: "close_pane".to_string(),
        }
    );
    let message = err.to_string();
    for needle in ["prefix+x", "split_vertical", "close_pane"] {
        assert!(message.contains(needle), "{message}");
    }
    let defaults = Keymap::defaults(HERDR_PREFIX);
    assert_eq!(defaults.lookup_prefix(&ch('x')), Some(Action::ClosePane));
    assert_eq!(
        defaults.lookup_prefix(&ch('v')),
        Some(Action::SplitVertical)
    );

    // Two overrides in one file colliding with each other.
    let err = Keymap::from_toml(
        "[bindings]\nzoom = \"prefix+f2\"\nresize_mode = \"prefix+f2\"\n",
        HERDR_PREFIX,
    )
    .unwrap_err();
    assert!(
        matches!(&err, KeymapError::Collision { chord, .. } if chord == "prefix+f2"),
        "{err:?}"
    );
    // A direct chord collides too.
    let err = Keymap::from_toml("[bindings]\nnavigate_down = \"up\"\n", HERDR_PREFIX).unwrap_err();
    assert_eq!(
        err,
        KeymapError::Collision {
            chord: "up".to_string(),
            action: "navigate_down".to_string(),
            displaced: "navigate_up".to_string(),
        }
    );

    // Moving the displaced action in the same file is accepted.
    let ok = Keymap::from_toml(
        "[bindings]\nsplit_vertical = \"prefix+x\"\nclose_pane = \"prefix+f3\"\n",
        HERDR_PREFIX,
    )
    .unwrap();
    assert_eq!(ok.lookup_prefix(&ch('x')), Some(Action::SplitVertical));
    assert_eq!(
        ok.lookup_prefix(&key(KeyCode::F(3), KeyModifiers::NONE)),
        Some(Action::ClosePane)
    );
    assert_eq!(ok.lookup_prefix(&ch('v')), None);

    // A chord owned only by a reserved action is free to take.
    let ok = Keymap::from_toml("[bindings]\nzoom = \"prefix+m\"\n", HERDR_PREFIX).unwrap();
    assert_eq!(ok.lookup_prefix(&ch('m')), Some(Action::Zoom));
    assert_eq!(ok.lookup_prefix(&ch('z')), None);

    // After any accepted merge every active chord dispatches to exactly one action.
    for keymap in [&defaults, &ok] {
        let chords = keymap.active_chords();
        let unique: HashSet<Trigger> = chords.iter().map(|(t, _)| *t).collect();
        assert_eq!(unique.len(), chords.len(), "duplicate active chord");
        assert!(chords.iter().all(|(_, action)| !action.is_reserved()));
    }
}

/// 4.1.1: every default binding names an action the live loop dispatches,
/// `edit_scrollback` is gone, and `custom_command` is the only reserved one.
#[test]
fn default_bindings_cover_every_action_except_reserved() {
    let defaults = Keymap::defaults(HERDR_PREFIX);
    for spec in BINDINGS {
        let action = Action::from_name(spec.name, spec.indexed.then_some(1))
            .unwrap_or_else(|| panic!("{} names no Action", spec.name));
        assert_eq!(action.name(), spec.name);
        assert_eq!(
            action.is_reserved(),
            spec.name == "custom_command",
            "{}",
            spec.name
        );
        assert_eq!(
            defaults.binding(spec.name).unwrap().reserved,
            action.is_reserved(),
            "{}",
            spec.name
        );
    }
    assert_eq!(Action::from_name("edit_scrollback", None), None);
    assert!(defaults.binding("edit_scrollback").is_none());
    assert_eq!(defaults.lookup_prefix(&ch('e')), None, "prefix+e is free");
    let err = Keymap::from_toml("[bindings]\nedit_scrollback = \"prefix+e\"\n", HERDR_PREFIX)
        .unwrap_err();
    assert_eq!(
        err,
        KeymapError::UnknownAction("edit_scrollback".to_string())
    );
}

/// 4.2.2: `load_keymap` on the resolved path is the only keymap source at
/// startup: the override chord wins over the default chord for the same
/// action, and the path follows `[keymap] path` when prefs set it.
#[test]
fn override_chord_replaces_default_chord() {
    let home = tempfile::tempdir().unwrap();
    let mut prefs = ClientPrefs::default();

    // No `[keymap] path`: the client-local file under the gobby home.
    assert_eq!(
        keymap_override_path(&prefs, home.path()),
        home.path().join("client").join("keymap.toml")
    );
    assert_eq!(
        keymap_override_path(&prefs, home.path()),
        default_override_path(home.path())
    );
    // A relative path lands under the gobby home; an absolute one is used as is.
    prefs.keybinds = "keys.toml".to_string();
    assert_eq!(
        keymap_override_path(&prefs, home.path()),
        home.path().join("keys.toml")
    );
    let elsewhere = tempfile::tempdir().unwrap();
    let absolute = elsewhere.path().join("mine.toml");
    prefs.keybinds = absolute.display().to_string();
    assert_eq!(keymap_override_path(&prefs, home.path()), absolute);

    // The override chord replaces the default chord for the same action.
    fs::write(&absolute, "[bindings]\nsettings = \"prefix+f4\"\n").unwrap();
    let keymap = load_keymap(&prefs, home.path(), false).unwrap();
    assert_eq!(
        keymap.lookup_prefix(&key(KeyCode::F(4), KeyModifiers::NONE)),
        Some(Action::Settings)
    );
    assert_eq!(
        keymap.lookup_prefix(&ch('s')),
        None,
        "default chord released"
    );

    // A missing file is the default keymap.
    prefs.keybinds = "absent.toml".to_string();
    let keymap = load_keymap(&prefs, home.path(), false).unwrap();
    assert_eq!(
        chords_by_name(&keymap),
        chords_by_name(&Keymap::defaults(HERDR_PREFIX))
    );

    // A rejected file names its resolved path and the keymap error.
    let rejected = home.path().join("absent.toml");
    fs::write(&rejected, "[bindings]\nsettings = \"prefix+x\"\n").unwrap();
    let error = load_keymap(&prefs, home.path(), false).unwrap_err();
    assert!(matches!(error, StartupError::Keymap { .. }), "{error:?}");
    let message = error.to_string();
    assert!(
        message.contains(&rejected.display().to_string()),
        "{message}"
    );
    assert!(message.contains("close_pane"), "{message}");
}

/// 4.3.1: an outer tmux eats `ctrl+b`, so the default prefix shifts to
/// `ctrl+]` while nested and the whole prefix table stays reachable behind
/// it; a `prefix` key in the override file wins, and without nesting herdr's
/// `ctrl+b` stands.
#[test]
fn nested_tmux_shifts_the_prefix_unless_overridden() {
    let ctrl = |c: char| key(KeyCode::Char(c), KeyModifiers::CONTROL);
    assert_eq!(default_prefix(false), HERDR_PREFIX);
    assert_eq!(default_prefix(true), "ctrl+]");

    let nested = Keymap::defaults(default_prefix(true));
    assert!(nested.is_prefix(&ctrl(']')));
    assert!(!nested.is_prefix(&ctrl('b')));
    assert_eq!(nested.prefix_label, "ctrl+]");
    for (event, action) in [
        (ch('?'), Action::Help),
        (ch('s'), Action::Settings),
        (ch('1'), Action::SwitchTab(1)),
        (ch('n'), Action::NextTab),
        (ch('p'), Action::PreviousTab),
        (ch('u'), Action::ReleaseControl),
    ] {
        assert_eq!(
            nested.lookup_prefix(&event),
            Some(action),
            "{action:?} must sit behind ctrl+]"
        );
    }
    assert_eq!(
        chords_by_name(&nested),
        chords_by_name(&Keymap::defaults(HERDR_PREFIX)),
        "only the prefix moves"
    );

    // The override file's own `prefix` wins over the nested default.
    let home = tempfile::tempdir().unwrap();
    let mut prefs = ClientPrefs::default();
    let client_dir = home.path().join("client");
    fs::create_dir_all(&client_dir).unwrap();
    fs::write(client_dir.join("keymap.toml"), "prefix = \"ctrl+a\"\n").unwrap();
    let overridden = load_keymap(&prefs, home.path(), true).unwrap();
    assert!(overridden.is_prefix(&ctrl('a')));
    assert!(!overridden.is_prefix(&ctrl(']')));

    // Without a file the nesting decides; without nesting it stays ctrl+b.
    prefs.keybinds = "absent.toml".to_string();
    let nested = load_keymap(&prefs, home.path(), true).unwrap();
    assert!(nested.is_prefix(&ctrl(']')));
    let plain = load_keymap(&prefs, home.path(), false).unwrap();
    assert!(plain.is_prefix(&ctrl('b')));
    assert!(!plain.is_prefix(&ctrl(']')));
}
