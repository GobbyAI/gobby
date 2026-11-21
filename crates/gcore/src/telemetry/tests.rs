use super::*;

const REQUESTS: &str = "requests_total";
const LATENCY: &str = "latency_seconds";
const DEPTH: &str = "queue_depth";
const TOOL_CALLS: &str = "tool_calls_total";

fn labels(pairs: &[(&'static str, &str)]) -> Vec<(&'static str, String)> {
    pairs
        .iter()
        .map(|(key, value)| (*key, value.to_string()))
        .collect()
}

fn series<'a>(snapshot: &'a [Sample], name: &str) -> Vec<&'a Sample> {
    snapshot
        .iter()
        .filter(|sample| sample.name == name)
        .collect()
}

#[test]
fn counter_series_read_back_by_name_and_labels() {
    let telemetry = Telemetry::new();
    telemetry.inc_counter(REQUESTS, &[("surface", "hooks")], 1);
    telemetry.inc_counter(REQUESTS, &[("surface", "hooks")], 1);
    telemetry.inc_counter(REQUESTS, &[("surface", "hooks")], 3);
    telemetry.inc_counter(REQUESTS, &[("surface", "mcp")], 1);

    assert_eq!(
        telemetry.snapshot(),
        vec![
            Sample {
                name: REQUESTS,
                labels: labels(&[("surface", "hooks")]),
                value: SampleValue::Counter(5),
            },
            Sample {
                name: REQUESTS,
                labels: labels(&[("surface", "mcp")]),
                value: SampleValue::Counter(1),
            },
        ]
    );
}

#[test]
fn histogram_observation_reads_back_count_and_sum() {
    let telemetry = Telemetry::new();
    telemetry.observe_histogram(LATENCY, &[("route", "/api/hooks")], 0.25);
    telemetry.observe_histogram(LATENCY, &[("route", "/api/hooks")], 0.75);

    assert_eq!(
        telemetry.snapshot(),
        vec![Sample {
            name: LATENCY,
            labels: labels(&[("route", "/api/hooks")]),
            value: SampleValue::Histogram { count: 2, sum: 1.0 },
        }]
    );
}

#[test]
fn snapshot_keeps_label_sets_apart_and_sorted() {
    let telemetry = Telemetry::new();
    // Recorded out of order: the snapshot sorts by name, then by labels.
    telemetry.inc_counter(REQUESTS, &[("surface", "mcp")], 2);
    telemetry.observe_histogram(LATENCY, &[("route", "/b")], 1.0);
    telemetry.inc_counter(REQUESTS, &[("surface", "daemon")], 4);
    telemetry.observe_histogram(LATENCY, &[("route", "/a")], 2.0);

    let order: Vec<(&str, Vec<(&'static str, String)>)> = telemetry
        .snapshot()
        .into_iter()
        .map(|sample| (sample.name, sample.labels))
        .collect();
    assert_eq!(
        order,
        vec![
            (LATENCY, labels(&[("route", "/a")])),
            (LATENCY, labels(&[("route", "/b")])),
            (REQUESTS, labels(&[("surface", "daemon")])),
            (REQUESTS, labels(&[("surface", "mcp")])),
        ]
    );
    let snapshot = telemetry.snapshot();
    let requests = series(&snapshot, REQUESTS);
    assert_eq!(requests[0].value, SampleValue::Counter(4));
    assert_eq!(requests[1].value, SampleValue::Counter(2));
}

#[test]
fn label_sets_beyond_the_cap_fold_into_overflow() {
    let telemetry = Telemetry::new();
    for index in 0..300 {
        // Each label value is built at run time and dropped after the call, so the
        // registry must own what it keeps.
        let tool = format!("tool-{index:03}");
        telemetry.inc_counter(TOOL_CALLS, &[("tool", tool.as_str())], 1);
    }

    let snapshot = telemetry.snapshot();
    let calls = series(&snapshot, TOOL_CALLS);
    assert_eq!(
        calls.len(),
        MAX_LABEL_SETS + 1,
        "256 kept series plus overflow"
    );
    let overflow: Vec<&&Sample> = calls
        .iter()
        .filter(|sample| sample.labels == labels(&[("overflow", "true")]))
        .collect();
    assert_eq!(overflow.len(), 1);
    assert_eq!(overflow[0].value, SampleValue::Counter(300 - 256));
    let kept = calls
        .iter()
        .filter(|sample| sample.labels != labels(&[("overflow", "true")]))
        .filter(|sample| sample.value == SampleValue::Counter(1))
        .count();
    assert_eq!(kept, MAX_LABEL_SETS);
}

#[test]
fn gauge_reads_back_last_value() {
    let telemetry = Telemetry::new();
    telemetry.set_gauge(DEPTH, &[("queue", "audit")], 3.0);
    telemetry.set_gauge(DEPTH, &[("queue", "audit")], 7.5);

    assert_eq!(
        telemetry.snapshot(),
        vec![Sample {
            name: DEPTH,
            labels: labels(&[("queue", "audit")]),
            value: SampleValue::Gauge(7.5),
        }]
    );
}
