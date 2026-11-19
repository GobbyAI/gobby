//! Waiver matching for the four case kinds (design-v2 section 7.7 and 7.8).
//!
//! The vector file is shared with the Python matcher in `scripts/parity_waivers.py`, so both
//! engines must return the same answer for every row. Cited cases run against fixture manifests
//! parsed in the test; the committed manifest is never read or changed here.

use gobby_daemon::front_door::compare::Outcome;
use gobby_daemon::front_door::waivers::{
    CaseSurface, WaiverError, WaiverManifest, is_native_extension, surface_matches,
    surface_matches_case,
};
use serde_json::{Value, json};

const VECTORS: &str = include_str!("../../../tests/contracts/waiver_match_vectors.json");
const WAIVER_ID: &str = "parity.fixture-case.surface-match";
const HTTP_PATCH: &str = "HTTP PATCH /api/cron/jobs/{job_id}";
const MCP_HTTP: &str = "MCP HTTP gobby:fixture_read";
const MCP_STDIO: &str = "MCP stdio gobby:fixture_read";
const WS_EVENT: &str = "WS message fixture_event";
const WS_OTHER: &str = "WS message fixture_other";
const CLI_LIST: &str = "CLI gobby fixture list";
const CLI_PARENT: &str = "CLI gobby fixture";

fn vectors() -> Value {
    serde_json::from_str(VECTORS).expect("waiver_match_vectors.json parses")
}

fn rows(vectors: &Value, list: &str) -> Vec<Value> {
    vectors[list]
        .as_array()
        .unwrap_or_else(|| panic!("vector list {list} is an array"))
        .clone()
}

fn text<'a>(row: &'a Value, field: &str) -> &'a str {
    row[field]
        .as_str()
        .unwrap_or_else(|| panic!("vector row field {field} is text"))
}

/// A fixture manifest with one waiver per `(id, surface)` pair, parsed the way the committed file is.
fn manifest(entries: &[(&str, &str)]) -> WaiverManifest {
    let waivers: Vec<Value> = entries
        .iter()
        .map(|(id, surface)| {
            json!({
                "id": id,
                "surface": surface,
                "behaviour": "fixture divergence for the surface matcher",
                "decision": "#25938",
                "replacement": "fixture replacement for the surface matcher",
            })
        })
        .collect();
    WaiverManifest::parse(&json!({"schema_version": 1, "waivers": waivers}).to_string())
        .expect("fixture waiver manifest parses")
}

/// Design section 7.8 for a cited case, restated over the production matcher: the cited waiver
/// must cover the case (else the citation errors), and only the native answer waives it.
fn cited_outcome(
    manifest: &WaiverManifest,
    waiver_id: &str,
    case: CaseSurface<'_>,
    native: &Value,
    actual: &Value,
) -> Result<Outcome, WaiverError> {
    manifest.cite(waiver_id, case)?;
    Ok(match (actual == native, is_native_extension(waiver_id)) {
        (false, _) => Outcome::Diff,
        (true, true) => Outcome::NativeExtension,
        (true, false) => Outcome::Waived,
    })
}

#[test]
fn vector_file_pins_the_shared_shape() {
    let vectors = vectors();
    assert_eq!(vectors["schema_version"], 1);
    let http = rows(&vectors, "http");
    let keyed = rows(&vectors, "keyed");
    assert!(!http.is_empty(), "the http vector list must not be empty");
    assert!(!keyed.is_empty(), "the keyed vector list must not be empty");
    for row in &http {
        let mut fields: Vec<&str> = row
            .as_object()
            .expect("row object")
            .keys()
            .map(String::as_str)
            .collect();
        fields.sort_unstable();
        assert_eq!(fields, ["expect", "method", "path", "surface"]);
        assert!(row["expect"].is_boolean(), "expect is a boolean");
    }
    for row in &keyed {
        let mut fields: Vec<&str> = row
            .as_object()
            .expect("row object")
            .keys()
            .map(String::as_str)
            .collect();
        fields.sort_unstable();
        assert_eq!(fields, ["expect", "key", "kind", "surface"]);
        assert!(row["expect"].is_boolean(), "expect is a boolean");
        assert!(
            matches!(text(row, "kind"), "mcp" | "ws" | "cli"),
            "a keyed row names an mcp, ws or cli kind: {row}"
        );
    }
    let kinds: Vec<&str> = keyed.iter().map(|row| text(row, "kind")).collect();
    for kind in ["mcp", "ws", "cli"] {
        assert!(kinds.contains(&kind), "keyed vectors cover kind {kind}");
    }
}

#[test]
fn http_vectors_keep_the_template_rule() {
    for row in rows(&vectors(), "http") {
        let (surface, method, path) = (
            text(&row, "surface"),
            text(&row, "method"),
            text(&row, "path"),
        );
        let expect = row["expect"].as_bool().expect("expect is a boolean");
        assert_eq!(surface_matches(surface, method, path), expect, "{row}");
        let case = CaseSurface::Http { method, path };
        assert_eq!(surface_matches_case(surface, case), expect, "{row}");
    }
}

#[test]
fn keyed_vectors_require_the_exact_ledger_key() {
    for row in rows(&vectors(), "keyed") {
        let (surface, key) = (text(&row, "surface"), text(&row, "key"));
        let expect = row["expect"].as_bool().expect("expect is a boolean");
        let case = CaseSurface::Keyed { key };
        assert_eq!(surface_matches_case(surface, case), expect, "{row}");
    }
}

#[test]
fn http_case_never_matches_a_keyed_surface() {
    // Ledger keys always carry their kind prefix, so a keyed surface never fits the HTTP template.
    assert!(!surface_matches_case(
        MCP_HTTP,
        CaseSurface::Http {
            method: "GET",
            path: "/api/x",
        }
    ));
}

#[test]
fn unknown_waiver_is_a_typed_error() {
    let waivers = manifest(&[(WAIVER_ID, CLI_LIST)]);
    let error = waivers
        .cite(
            "parity.missing.surface-match",
            CaseSurface::Keyed { key: CLI_LIST },
        )
        .expect_err("an unknown waiver id must not cite");
    assert_eq!(
        error,
        WaiverError::UnknownWaiver("parity.missing.surface-match".to_owned())
    );
    assert!(error.to_string().contains("unknown waiver"), "{error}");
}

#[test]
fn waiver_error_display_keeps_the_assertion_substrings() {
    let mismatch = WaiverError::SurfaceMismatch {
        case: "PATCH /api/cron/jobs/job-1".to_owned(),
        surface: MCP_HTTP.to_owned(),
    };
    assert!(
        mismatch.to_string().contains("waiver surface"),
        "{mismatch}"
    );
    assert!(mismatch.to_string().contains(MCP_HTTP), "{mismatch}");
}

#[test]
fn cited_case_of_each_kind_resolves_to_its_own_waiver() {
    let cases = [
        (
            HTTP_PATCH,
            CaseSurface::Http {
                method: "PATCH",
                path: "/api/cron/jobs/job-1",
            },
        ),
        (MCP_HTTP, CaseSurface::Keyed { key: MCP_HTTP }),
        (MCP_STDIO, CaseSurface::Keyed { key: MCP_STDIO }),
        (WS_EVENT, CaseSurface::Keyed { key: WS_EVENT }),
        (CLI_LIST, CaseSurface::Keyed { key: CLI_LIST }),
    ];
    for (surface, case) in cases {
        let waivers = manifest(&[(WAIVER_ID, surface)]);
        let entry = waivers
            .cite(WAIVER_ID, case)
            .expect("the case cites its own waiver");
        assert_eq!(entry.surface, surface);
    }
}

#[test]
fn cited_case_of_each_kind_is_waived_only_on_its_native_answer() {
    let native = json!({"status": 200, "body": "native"});
    let python = json!({"status": 200, "body": "python"});
    let other = json!({"status": 500, "body": "other"});
    let cases = [
        (
            HTTP_PATCH,
            CaseSurface::Http {
                method: "PATCH",
                path: "/api/cron/jobs/job-1",
            },
        ),
        (MCP_HTTP, CaseSurface::Keyed { key: MCP_HTTP }),
        (MCP_STDIO, CaseSurface::Keyed { key: MCP_STDIO }),
        (WS_EVENT, CaseSurface::Keyed { key: WS_EVENT }),
        (CLI_LIST, CaseSurface::Keyed { key: CLI_LIST }),
    ];
    for (surface, case) in cases {
        let waivers = manifest(&[(WAIVER_ID, surface)]);
        let outcome = |actual: &Value| cited_outcome(&waivers, WAIVER_ID, case, &native, actual);
        assert_eq!(outcome(&native), Ok(Outcome::Waived), "{surface}");
        assert_eq!(outcome(&python), Ok(Outcome::Diff), "{surface}");
        assert_eq!(outcome(&other), Ok(Outcome::Diff), "{surface}");
    }
}

#[test]
fn keyed_case_naming_another_surface_is_a_surface_mismatch() {
    let mismatched = [
        (MCP_STDIO, MCP_HTTP),
        (WS_EVENT, WS_OTHER),
        (CLI_LIST, CLI_PARENT),
    ];
    for (surface, key) in mismatched {
        let waivers = manifest(&[(WAIVER_ID, surface)]);
        let error = waivers
            .cite(WAIVER_ID, CaseSurface::Keyed { key })
            .expect_err("a foreign key must not cite");
        assert_eq!(
            error,
            WaiverError::SurfaceMismatch {
                case: key.to_owned(),
                surface: surface.to_owned(),
            }
        );
    }
}

#[test]
fn cross_kind_citations_are_surface_mismatches() {
    let http_case_citing_keyed = manifest(&[(WAIVER_ID, MCP_HTTP)]);
    let error = http_case_citing_keyed
        .cite(
            WAIVER_ID,
            CaseSurface::Http {
                method: "PATCH",
                path: "/api/cron/jobs/job-1",
            },
        )
        .expect_err("an HTTP case must not cite a keyed waiver");
    assert_eq!(
        error,
        WaiverError::SurfaceMismatch {
            case: "PATCH /api/cron/jobs/job-1".to_owned(),
            surface: MCP_HTTP.to_owned(),
        }
    );

    let keyed_case_citing_http = manifest(&[(WAIVER_ID, HTTP_PATCH)]);
    let error = keyed_case_citing_http
        .cite(WAIVER_ID, CaseSurface::Keyed { key: CLI_LIST })
        .expect_err("a keyed case must not cite an HTTP waiver");
    assert_eq!(
        error,
        WaiverError::SurfaceMismatch {
            case: CLI_LIST.to_owned(),
            surface: HTTP_PATCH.to_owned(),
        }
    );
}

#[test]
fn one_manifest_holds_all_four_kinds() {
    let entries = [
        ("parity.http-case.surface-match", HTTP_PATCH),
        ("parity.mcp-case.surface-match", MCP_HTTP),
        ("parity.ws-case.surface-match", WS_EVENT),
        ("parity.cli-case.surface-match", CLI_LIST),
    ];
    let waivers = manifest(&entries);
    let cited = [
        (
            "parity.http-case.surface-match",
            CaseSurface::Http {
                method: "PATCH",
                path: "/api/cron/jobs/job-1",
            },
        ),
        (
            "parity.mcp-case.surface-match",
            CaseSurface::Keyed { key: MCP_HTTP },
        ),
        (
            "parity.ws-case.surface-match",
            CaseSurface::Keyed { key: WS_EVENT },
        ),
        (
            "parity.cli-case.surface-match",
            CaseSurface::Keyed { key: CLI_LIST },
        ),
    ];
    for (id, case) in cited {
        let entry = waivers
            .cite(id, case)
            .expect("each case cites its own waiver");
        assert_eq!(entry.id, id);
    }
}
