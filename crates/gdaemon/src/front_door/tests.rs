use super::*;

#[test]
fn observe_peer_replaces_a_spoofed_peer_port_with_the_accepted_socket() {
    let mut headers = HeaderMap::new();
    headers.insert(
        HeaderName::from_static(PEER_PORT_HEADER),
        HeaderValue::from_static("1"),
    );
    let peer: SocketAddr = "127.0.0.1:53412".parse().expect("socket address");

    observe_peer(&mut headers, peer, false);

    let ports: Vec<&HeaderValue> = headers.get_all(PEER_PORT_HEADER).iter().collect();
    assert_eq!(ports, [&HeaderValue::from_static("53412")]);
}
