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

#[test]
fn entry_strip_removes_forged_identity_headers() {
    let mut headers = HeaderMap::new();
    for name in gobby_core::family::IDENTITY_ENTRY_HEADERS {
        headers.insert(
            HeaderName::from_static(name),
            HeaderValue::from_static("forged"),
        );
    }
    let peer: SocketAddr = "127.0.0.1:53412".parse().expect("socket address");

    observe_peer(&mut headers, peer, false);

    let survivors: Vec<&str> = gobby_core::family::IDENTITY_ENTRY_HEADERS
        .into_iter()
        .filter(|name| headers.contains_key(*name))
        .collect();
    assert_eq!(survivors, Vec::<&str>::new());
}

#[test]
fn entry_strip_keeps_the_front_door_proof() {
    let mut headers = HeaderMap::new();
    headers.insert(
        HeaderName::from_static("x-gobby-front-door"),
        HeaderValue::from_static("front-door-proof"),
    );
    headers.insert(
        HeaderName::from_static("x-gobby-backend-token"),
        HeaderValue::from_static("backend-token"),
    );
    let peer: SocketAddr = "127.0.0.1:53412".parse().expect("socket address");

    observe_peer(&mut headers, peer, false);

    assert_eq!(
        headers.get("x-gobby-front-door"),
        Some(&HeaderValue::from_static("front-door-proof"))
    );
    assert_eq!(
        headers.get("x-gobby-backend-token"),
        Some(&HeaderValue::from_static("backend-token"))
    );
}
