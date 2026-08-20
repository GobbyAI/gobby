use super::*;

#[test]
fn buffers_split_csi_sequences() {
    let mut tracker = KittyKeyboardTracker::default();

    tracker.observe(b"\x1b[>1u\x1b[>4;");
    tracker.observe(b"01m\x1b[>5u\x1b[<");
    tracker.observe(b"u");

    assert_eq!(tracker.flags, 1);
    assert_eq!(tracker.stack, vec![0]);
    #[cfg(windows)]
    {
        assert!(tracker.modify_other_keys_enabled());
        tracker.observe(b"\x1b[>1m");
        assert!(tracker.modify_other_keys_enabled());
        tracker.observe(b"\x1b[>04n");
        assert!(!tracker.modify_other_keys_enabled());
    }
}
