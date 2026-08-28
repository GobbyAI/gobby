use super::*;

fn response_bytes(responses: Vec<XtgettcapResponse>) -> Vec<Bytes> {
    responses
        .into_iter()
        .map(|response| response.bytes)
        .collect()
}

#[test]
fn tracker_returns_multiple_capabilities_in_order() {
    let mut tracker = XtgettcapQueryTracker::default();

    tracker.observe(b"\x1bP+q5463;524742\x1b\\");

    assert_eq!(
        response_bytes(tracker.drain_pending()),
        vec![
            Bytes::from_static(b"\x1bP1+r5463\x1b\\"),
            Bytes::from_static(b"\x1bP1+r524742=38\x1b\\"),
        ]
    );
}

#[test]
fn tracker_normalizes_mixed_case_query_keys() {
    let mut tracker = XtgettcapQueryTracker::default();

    tracker.observe(b"\x1bP+q4d73\x1b\\");

    assert_eq!(
        response_bytes(tracker.drain_pending()),
        vec![Bytes::from_static(
            b"\x1bP1+r4D73=5C455D35323B25703125733B25703225735C303037\x1b\\"
        )]
    );
}

#[test]
fn tracker_ignores_unsupported_capabilities() {
    let mut tracker = XtgettcapQueryTracker::default();

    tracker.observe(b"\x1bP+q6E6F7065\x1b\\");

    assert!(response_bytes(tracker.drain_pending()).is_empty());
}

#[test]
fn tracker_returns_underline_style_capability() {
    let mut tracker = XtgettcapQueryTracker::default();

    tracker.observe(b"\x1bP+q536D756C78\x1b\\");

    assert_eq!(
        response_bytes(tracker.drain_pending()),
        vec![Bytes::from_static(
            b"\x1bP1+r536D756C78=5C455B343A25703125646D\x1b\\"
        )]
    );
}

#[test]
fn tracker_keeps_split_query_until_string_terminator() {
    let mut tracker = XtgettcapQueryTracker::default();

    tracker.observe(b"\x1bP+q537");
    assert!(response_bytes(tracker.drain_pending()).is_empty());
    tracker.observe(b"5\x1b");
    assert!(response_bytes(tracker.drain_pending()).is_empty());
    tracker.observe(b"\\");

    assert_eq!(
        response_bytes(tracker.drain_pending()),
        vec![Bytes::from_static(b"\x1bP1+r5375\x1b\\")]
    );
}

#[test]
fn tracker_resumes_after_ignored_osc_bel_terminator() {
    let mut tracker = XtgettcapQueryTracker::default();

    tracker.observe(b"\x1b]0;title\x07\x1bP+q5463\x1b\\");

    assert_eq!(
        response_bytes(tracker.drain_pending()),
        vec![Bytes::from_static(b"\x1bP1+r5463\x1b\\")]
    );
}

#[test]
fn tracker_accepts_eight_bit_dcs_and_string_terminator() {
    let mut tracker = XtgettcapQueryTracker::default();

    tracker.observe(b"\x90+q5463\x9c");

    assert_eq!(
        response_bytes(tracker.drain_pending()),
        vec![Bytes::from_static(b"\x1bP1+r5463\x1b\\")]
    );
}

#[test]
fn tracker_ignores_xtgettcap_bytes_inside_eight_bit_osc() {
    let mut tracker = XtgettcapQueryTracker::default();

    tracker.observe(b"\x9dtitle\x1bP+q5463\x9c\x1bP+q5463\x1b\\");

    assert_eq!(
        response_bytes(tracker.drain_pending()),
        vec![Bytes::from_static(b"\x1bP1+r5463\x1b\\")]
    );
}

#[test]
fn tracker_reports_response_end_offsets() {
    let mut tracker = XtgettcapQueryTracker::default();

    tracker.observe(b"before\x1bP+q5463\x1b\\after");

    assert_eq!(
        tracker.drain_pending(),
        vec![XtgettcapResponse {
            end_offset: 16,
            bytes: Bytes::from_static(b"\x1bP1+r5463\x1b\\"),
        }]
    );
}
