use super::{load_prefs, prefs_path, save_prefs};
use crate::ui::settings::ClientPrefs;

/// A gobby home whose prefs file holds `text`.
fn home_with_prefs(text: &str) -> tempfile::TempDir {
    let home = tempfile::tempdir().expect("temp home");
    let path = prefs_path(home.path());
    std::fs::create_dir_all(path.parent().expect("prefs dir")).expect("client dir");
    std::fs::write(&path, text).expect("write prefs");
    home
}

#[test]
fn saved_section_heights_load_back_under_the_ui_table() {
    let home = tempfile::tempdir().expect("temp home");
    let prefs = ClientPrefs {
        sidebar_section_heights: [12, 3, 17, 8],
        ..ClientPrefs::default()
    };
    let path = save_prefs(home.path(), &prefs).expect("save prefs");
    let saved: toml::Value =
        toml::from_str(&std::fs::read_to_string(&path).expect("read prefs")).expect("parse prefs");
    let stored: Vec<i64> = saved["ui"]
        .get("sidebar_section_heights")
        .and_then(toml::Value::as_array)
        .expect("section heights are saved under [ui]")
        .iter()
        .filter_map(toml::Value::as_integer)
        .collect();
    assert_eq!(stored, vec![12, 3, 17, 8]);

    let loaded = load_prefs(home.path()).expect("load prefs");
    assert_eq!(loaded.sidebar_section_heights, [12, 3, 17, 8]);
}

#[test]
fn invalid_stored_section_heights_fall_back_to_derived_per_entry() {
    // A string, a zero, and a value past u16::MAX each load as 0.
    let home = home_with_prefs("[ui]\nsidebar_section_heights = [12, \"tall\", 0, 70000]\n");
    let loaded = load_prefs(home.path()).expect("a bad height is not a parse error");
    assert_eq!(loaded.sidebar_section_heights, [12, 0, 0, 0]);
}

#[test]
fn a_section_heights_value_that_is_not_an_array_loads_as_all_derived() {
    let home = home_with_prefs("[ui]\nsidebar_section_heights = 9\n");
    let loaded = load_prefs(home.path()).expect("a bad height is not a parse error");
    assert_eq!(loaded.sidebar_section_heights, [0; 4]);
}

#[test]
fn a_prefs_file_without_section_heights_loads_as_all_derived() {
    let home = home_with_prefs("[ui]\nmonochrome = false\n");
    let loaded = load_prefs(home.path()).expect("a missing height is not a parse error");
    assert_eq!(loaded.sidebar_section_heights, [0; 4]);
}
