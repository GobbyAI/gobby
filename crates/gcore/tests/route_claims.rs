//! Claim matching: the method, the path shape and the raw-path rules of a route claim.

use axum::http::Method;
use gobby_core::route_family::{Credential, Gate, Kind, PathMatch, PeerRule, RouteClaim};

fn claim(method: Option<Method>, path: PathMatch) -> RouteClaim {
    RouteClaim {
        method,
        path,
        gate: Gate::BeforeRefusal,
        credential: Credential::Unchecked,
        peer: PeerRule::Any,
        kind: Kind::Any,
    }
}

#[test]
fn route_claim_exact_matches_method_and_path() {
    let get_only = claim(Some(Method::GET), PathMatch::Exact("/x"));
    assert!(
        get_only.matches(&Method::GET, "/x", false),
        "GET /x matches"
    );
    assert!(
        !get_only.matches(&Method::POST, "/x", false),
        "POST /x does not match a GET row"
    );
    assert!(
        !get_only.matches(&Method::GET, "/x/extra", false),
        "an exact row does not match a longer path"
    );
}

#[test]
fn route_claim_matchers_keep_today_rules() {
    let segment = claim(None, PathMatch::Segment("/api/machines"));
    assert!(segment.matches(&Method::GET, "/api/machines/x", false));
    assert!(!segment.matches(&Method::GET, "/api/machinesx", false));

    let raw_prefix = claim(None, PathMatch::RawPrefix("/_gobby/"));
    assert!(raw_prefix.matches(&Method::GET, "/_gobby/ws/bind", false));
    assert!(!raw_prefix.matches(&Method::GET, "/_gobby", false));

    let any = claim(None, PathMatch::Any);
    assert!(any.matches(&Method::GET, "/anything", false));
    assert!(any.matches(&Method::POST, "/", true));
}

#[test]
fn upgrade_only_row_answers_upgrades_and_refuses_plain_requests() {
    let upgrade_only = RouteClaim {
        kind: Kind::UpgradeOnly,
        ..claim(None, PathMatch::Any)
    };
    assert!(
        upgrade_only.matches(&Method::GET, "/ws", true),
        "an upgrade matches an upgrade-only row"
    );
    assert!(
        !upgrade_only.matches(&Method::GET, "/ws", false),
        "a plain request does not match an upgrade-only row"
    );
}
