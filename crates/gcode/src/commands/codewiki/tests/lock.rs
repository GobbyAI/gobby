use super::*;

#[test]
fn second_sink_on_same_out_dir_is_refused_while_the_first_holds_the_lock() {
    let project = tempfile::tempdir().expect("project tempdir");
    let out_dir = project.path().join("wiki");
    let _held = DocSink::open(project.path(), &out_dir, "symbols").expect("first sink opens");

    let refused = DocSink::open(project.path(), &out_dir, "symbols")
        .expect_err("second sink on the same out_dir must be refused");

    assert!(
        refused
            .to_string()
            .contains("another gcode codewiki run is already writing"),
        "{refused}"
    );
}

#[test]
fn finish_releases_the_writer_lock_for_the_next_run() {
    let project = tempfile::tempdir().expect("project tempdir");
    let out_dir = project.path().join("wiki");
    let sink = DocSink::open(project.path(), &out_dir, "symbols").expect("first sink opens");
    sink.finish(None).expect("first run finishes");

    let reopened = DocSink::open(project.path(), &out_dir, "symbols");
    assert!(
        reopened.is_ok(),
        "next run must open after finish: {:?}",
        reopened.err()
    );
}
