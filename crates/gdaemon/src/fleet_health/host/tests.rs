use super::*;

const TABLE: &str = "\
   0.0 launchd
  63.5 fseventsd
   2.1 syspolicyd
   0.4 mds_stores
   1.0 rustc
   3.3 fseventsd
";

#[test]
fn process_table_reports_watched_processes_and_keeps_the_highest_fseventsd_row() {
    let snapshot = parse_process_table(TABLE);
    assert_eq!(snapshot.fseventsd_cpu_percent, Some(63.5));
    assert_eq!(snapshot.syspolicyd_cpu_percent, Some(2.1));
    assert_eq!(snapshot.mds_stores_cpu_percent, Some(0.4));
    assert!(snapshot.rustc_running);
}

#[test]
fn process_table_without_rustc_reports_it_not_running() {
    let snapshot = parse_process_table("  63.5 fseventsd\n   0.1 launchd\n");
    assert!(!snapshot.rustc_running);
}

#[test]
fn process_table_without_fseventsd_reports_no_reading() {
    let snapshot = parse_process_table("   0.1 launchd\n   1.0 rustc\n");
    assert_eq!(snapshot.fseventsd_cpu_percent, None);
    assert!(snapshot.rustc_running);
}

#[test]
fn process_table_ignores_header_and_malformed_rows() {
    let snapshot = parse_process_table("%CPU COMMAND\nnot-a-number fseventsd\n\n");
    assert_eq!(snapshot, ProcessSnapshot::default());
}

#[test]
fn memory_level_parses_percent_and_rejects_noise() {
    assert_eq!(parse_memory_free_percent("77\n"), Some(77.0));
    assert_eq!(parse_memory_free_percent("  9 \n"), Some(9.0));
    assert_eq!(parse_memory_free_percent("sysctl: unknown oid"), None);
    assert_eq!(parse_memory_free_percent(""), None);
}

#[test]
fn load_per_core_divides_by_cores() {
    assert_eq!(load_per_core(3.0, 4), 0.75);
    assert_eq!(load_per_core(0.0, 8), 0.0);
}
