use super::*;

#[test]
fn benchmark_requires_daemon() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("benchmark");
    let output = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "benchmark", "--topic", &topic.name],
    );
    assert!(
        !output.status.success(),
        "benchmark succeeded without a daemon"
    );
    assert_daemon_required(&output, "benchmark");
}
