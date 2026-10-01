use super::Coalescer;

#[test]
fn coalescer_keeps_one_in_flight_and_latest_pending() {
    let mut coalescer = Coalescer::default();
    assert_eq!(
        coalescer.offer("pane", 1),
        Some(1),
        "an idle key issues at once"
    );
    assert_eq!(
        coalescer.offer("pane", 2),
        None,
        "a busy key holds the offer"
    );
    assert_eq!(
        coalescer.offer("pane", 3),
        None,
        "a newer offer replaces the held one"
    );
    assert_eq!(
        coalescer.offer("other", 9),
        Some(9),
        "keys coalesce independently"
    );
    assert_eq!(
        coalescer.settle(&"pane"),
        Some(3),
        "settling releases only the latest offer"
    );
    assert_eq!(
        coalescer.offer("pane", 4),
        None,
        "the released offer is in flight now"
    );
    assert_eq!(coalescer.settle(&"pane"), Some(4));
    assert_eq!(
        coalescer.settle(&"pane"),
        None,
        "with nothing held the key goes idle"
    );
    assert_eq!(
        coalescer.offer("pane", 5),
        Some(5),
        "an idle key issues again"
    );
    assert_eq!(coalescer.settle(&"other"), None);
}
