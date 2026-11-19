//! The family-crate template and the activation runbook, as the docs state them, and a
//! family built from the template reaching the route table.

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::Router;
use axum::http::Method;
use gobby_core::bootstrap::RouteBackend;
use gobby_core::route_family::{
    Credential, Gate, Kind, PathMatch, PeerRule, RouteClaim, RouteFamily,
};
use gobby_daemon::front_door::FrontDoorState;
use gobby_daemon::front_door::auth::AuthState;
use gobby_daemon::front_door::health::BackendState;
use gobby_daemon::front_door::routes::RouteTable;

use super::common;

const CRATES_AGENTS: &str = include_str!("../../AGENTS.md");
const CONFIGURATION_GUIDE: &str = include_str!("../../../docs/guides/configuration.md");

/// The text from the line `heading` to the next line starting with `next`, with line
/// endings normalized, backticks removed and every whitespace run collapsed to a space.
fn section(document: &str, heading: &str, next: &str) -> String {
    let document = document.replace("\r\n", "\n");
    let start = document.lines().position(|line| line.trim_end() == heading);
    let Some(start) = start else {
        return String::new();
    };
    let body: Vec<&str> = document
        .lines()
        .skip(start + 1)
        .take_while(|line| !line.starts_with(next))
        .collect();
    body.join(" ")
        .replace('`', "")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn missing<'a>(text: &str, terms: &[&'a str]) -> Vec<&'a str> {
    terms
        .iter()
        .copied()
        .filter(|term| !text.contains(term))
        .collect()
}

#[test]
fn crates_agents_documents_the_family_contract() {
    let text = section(CRATES_AGENTS, "## Daemon family crates", "## ");
    let terms = [
        "RouteFamily",
        "FamilyContext",
        "claims",
        "check_proxied",
        "default_backend",
        "ProxiedCheck",
        "activation commit",
        "Found Work",
        "start",
        "stop",
        "drain",
        "degraded",
        "DomainError",
        "frozen",
        "before_spawn",
        "after_healthy",
        "recovery_complete",
        "start_rank",
        "Extensions",
        "one backend per family",
        "cut group",
        "families never edit serve.rs",
    ];
    assert_eq!(missing(&text, &terms), Vec::<&str>::new());
    // serve.rs is named only by that rule and by the Found Work rule for runner wiring.
    let stray: Vec<&str> = text
        .split(". ")
        .filter(|sentence| sentence.contains("serve.rs"))
        .filter(|sentence| {
            !sentence.contains("families never edit serve.rs") && !sentence.contains("Found Work")
        })
        .collect();
    assert_eq!(stray, Vec::<&str>::new());
}

#[test]
fn configuration_guide_states_compiled_defaults() {
    let text = section(CONFIGURATION_GUIDE, "#### front_door block", "## Nodes");
    assert_eq!(
        missing(
            &text,
            &[
                "compiled default",
                "activation commit",
                "generation",
                "gobby cutover"
            ]
        ),
        Vec::<&str>::new()
    );
    assert!(
        !text.contains("absent = proxy"),
        "the guide still says a family without a routes entry is proxied"
    );
}

/// A family as the template describes it: one claim row, native by default.
struct TemplateFamily;

static TEMPLATE_CLAIMS: &[RouteClaim] = &[RouteClaim {
    method: Some(Method::GET),
    path: PathMatch::Exact("/template/ping"),
    gate: Gate::AfterRefusal,
    credential: Credential::AfterAuthenticate,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

impl RouteFamily<FrontDoorState> for TemplateFamily {
    fn name(&self) -> &'static str {
        "template"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }

    fn default_backend(&self) -> RouteBackend {
        RouteBackend::Native
    }

    fn route_claims(&self) -> &'static [RouteClaim] {
        TEMPLATE_CLAIMS
    }
}

static TEMPLATE: TemplateFamily = TemplateFamily;

#[tokio::test]
async fn fixture_family_claims_through_the_route_table() {
    let auth = Arc::new(
        AuthState::new("boot-secret".into(), None, Default::default()).expect("auth state"),
    );
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [&TEMPLATE];

    let table = RouteTable::new(&families, &BTreeMap::new(), &state);
    assert_eq!(
        table.claim_owner(&Method::GET, "/template/ping", false),
        Some("template")
    );
    assert_eq!(
        table.claim_owner(&Method::POST, "/template/ping", false),
        None
    );

    // A routes entry that sends the family to the proxy leaves its claims unconsulted.
    let proxied = BTreeMap::from([("template".to_owned(), RouteBackend::Proxy)]);
    let table = RouteTable::new(&families, &proxied, &state);
    assert_eq!(
        table.claim_owner(&Method::GET, "/template/ping", false),
        None
    );
}
