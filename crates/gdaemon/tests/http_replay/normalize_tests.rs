use super::*;
use serde_json::json;
use time::format_description::well_known::Rfc3339;

const U1: &str = "11111111-1111-4111-8111-111111111111";
const U2: &str = "22222222-2222-4222-8222-222222222222";
/// A canonical UUID written in uppercase, which `uuid_ordinal` refuses (`_UUID` is lowercase).
const UPPER_UUID: &str = "AABBCCDD-EEFF-4011-8122-334455667788";

fn at(text: &str) -> OffsetDateTime {
    OffsetDateTime::parse(text, &Rfc3339).expect("rfc3339 test timestamp")
}

fn one_hour_window() -> Window {
    Window {
        start: at("2023-11-14T22:00:00Z"),
        end: at("2023-11-14T23:00:00Z"),
    }
}

#[test]
fn closed_set_names_parse_and_everything_else_is_refused() {
    assert_eq!(parse_name("uuid_ordinal"), Ok(Normalizer::UuidOrdinal));
    assert_eq!(parse_name("monotonic"), Ok(Normalizer::Monotonic));
    assert_eq!(parse_name("timestamp"), Ok(Normalizer::Timestamp));
    assert_eq!(parse_name("bytes"), Ok(Normalizer::Bytes));
    assert_eq!(parse_name("none"), Ok(Normalizer::NoOp));
    assert_eq!(parse_name("set"), Err(NormalizeError::PerTableSet));
    assert_eq!(
        parse_name("bogus"),
        Err(NormalizeError::UnknownName("bogus".to_owned()))
    );
}

#[test]
fn uuid_ordinals_number_by_first_appearance_in_document_order() {
    // Golden (Python normalize_document, exact pointers only): a/id is 1, then b/0/id is 2,
    // b/1/id is 1 and b/2/id is 2.
    let document = json!({
        "a": {"id": U1},
        "b": [{"id": U2}, {"id": U1}, {"id": U2}]
    });
    let rules = [
        ("/a/id".to_owned(), Normalizer::UuidOrdinal),
        ("/b/0/id".to_owned(), Normalizer::UuidOrdinal),
        ("/b/1/id".to_owned(), Normalizer::UuidOrdinal),
        ("/b/2/id".to_owned(), Normalizer::UuidOrdinal),
    ];
    let normalized =
        normalize_document(&document, &json!([]), &rules, &one_hour_window()).expect("normalizes");
    assert_eq!(
        normalized,
        json!({
            "a": {"id": "@uuid:1@"},
            "b": [{"id": "@uuid:2@"}, {"id": "@uuid:1@"}, {"id": "@uuid:2@"}]
        })
    );
}

#[test]
fn monotonic_accepts_decimal_text_as_pg_notify_delivers_payloads() {
    // Captures are one sequence unit; a payload is text, as pg_notify delivers it
    // (config_values_patch.json, /payload). Golden (Python normalize_captures): "9", "10" then
    // "-3" fails at item 2, and "9", "10" masks to @seq@.
    let rules = [(
        "/payload".to_owned(),
        parse_spec(&json!("monotonic"), false).expect("spec"),
    )];
    let captures = [
        json!({"payload": "9"}),
        json!({"payload": "10"}),
        json!({"payload": "-3"}),
    ];
    let error = normalize_items(&captures, &rules, true, "captures", &one_hour_window())
        .expect_err("-3 does not follow 10");
    assert_eq!(
        error,
        NormalizeError::NotMonotonic {
            pointer: "captures[2]/payload".to_owned()
        }
    );

    let rising = [json!({"payload": "9"}), json!({"payload": "10"})];
    assert_eq!(
        normalize_items(&rising, &rules, true, "captures", &one_hour_window())
            .expect("digit text strictly increases"),
        vec![json!({"payload": "@seq@"}), json!({"payload": "@seq@"})]
    );
}

#[test]
fn monotonic_refuses_values_that_are_not_integers() {
    // Golden (Python normalize_captures): every one of these is "monotonic needs an integer".
    let rules = [(
        "/seq".to_owned(),
        parse_spec(&json!("monotonic"), false).expect("spec"),
    )];
    for value in [
        json!("1.5"),
        json!("x1"),
        json!(""),
        json!(true),
        json!(2.0),
    ] {
        let error = normalize_items(
            &[json!({"seq": value})],
            &rules,
            true,
            "captures",
            &one_hour_window(),
        )
        .expect_err("not an integer");
        assert_eq!(
            error,
            NormalizeError::NotInteger {
                pointer: "captures[0]/seq".to_owned()
            }
        );
    }
}

#[test]
fn monotonic_sequences_mask_to_seq_and_must_strictly_increase() {
    // Golden (Python normalize_captures): 1, 2, 7 masks to @seq@; 2 then 1 fails at item 1.
    let rules = [(
        "/seq".to_owned(),
        parse_spec(&json!("monotonic"), false).expect("spec"),
    )];
    let rising = [json!({"seq": 1}), json!({"seq": 2}), json!({"seq": 7})];
    let normalized = normalize_items(&rising, &rules, true, "captures", &one_hour_window())
        .expect("strictly increasing");
    assert_eq!(
        normalized,
        vec![
            json!({"seq": "@seq@"}),
            json!({"seq": "@seq@"}),
            json!({"seq": "@seq@"})
        ]
    );

    let falling = [json!({"seq": 2}), json!({"seq": 1})];
    let error = normalize_items(&falling, &rules, true, "captures", &one_hour_window())
        .expect_err("reordered sequence");
    assert_eq!(
        error,
        NormalizeError::NotMonotonic {
            pointer: "captures[1]/seq".to_owned()
        }
    );
}

#[test]
fn timestamps_inside_the_window_mask_and_outside_fail() {
    let rules = [("/t".to_owned(), Normalizer::Timestamp)];
    let inside = json!({"t": "2023-11-14T22:30:00Z"});
    let normalized = normalize_document(&inside, &json!([]), &rules, &one_hour_window())
        .expect("inside the window");
    assert_eq!(normalized, json!({"t": "@ts@"}));

    let outside = json!({"t": "2023-11-15T00:00:00Z"});
    let error = normalize_document(&outside, &json!([]), &rules, &one_hour_window())
        .expect_err("outside the window");
    assert_eq!(
        error,
        NormalizeError::OutsideWindow {
            pointer: "/t".to_owned()
        }
    );
}

#[test]
fn masks_run_after_the_rules_and_redaction_is_the_callers_step() {
    // Golden (Python normalize_document, which takes no mask and does not redact): the uuid rule
    // numbers id as 1, the password is untouched, and the comparison masks id last.
    let document = json!({"id": U1, "password": "hunter-two"});
    let rules = [("/id".to_owned(), Normalizer::UuidOrdinal)];
    let normalized = normalize_document(&document, &json!(["/id"]), &rules, &one_hour_window())
        .expect("normalizes");
    assert_eq!(
        normalized,
        json!({"id": "@mask@", "password": "hunter-two"})
    );
}

#[test]
fn parse_spec_takes_names_and_an_after_only_on_a_db_timestamp() {
    assert_eq!(
        parse_spec(&json!("uuid_ordinal"), false),
        Ok(Spec {
            normalizer: Normalizer::UuidOrdinal,
            after: None
        })
    );
    assert_eq!(
        parse_spec(&json!({"name": "timestamp", "after": "created_at"}), true),
        Ok(Spec {
            normalizer: Normalizer::Timestamp,
            after: Some("created_at".to_owned())
        })
    );
    assert_eq!(
        parse_spec(&json!({"name": "timestamp", "after": null}), true),
        Ok(Spec {
            normalizer: Normalizer::Timestamp,
            after: None
        })
    );
    for refused in [
        (json!({"name": "timestamp", "after": "c"}), false),
        (json!({"name": "uuid_ordinal", "after": "c"}), true),
        (json!("set"), true),
        (json!("bogus"), true),
        (json!({"name": "timestamp", "extra": 1}), true),
        (json!({"after": "c"}), true),
        (json!(7), true),
    ] {
        let (spec, allow_after) = refused;
        assert!(
            matches!(
                parse_spec(&spec, allow_after),
                Err(NormalizeError::BadSpec(_))
            ),
            "{spec} must be refused"
        );
    }
}

#[test]
fn parse_order_takes_the_two_declared_orders_only() {
    assert_eq!(parse_order("set"), Ok(Order::Set));
    assert_eq!(parse_order("as_recorded"), Ok(Order::AsRecorded));
    assert_eq!(
        parse_order("sorted"),
        Err(NormalizeError::BadOrder("sorted".to_owned()))
    );
}

#[test]
fn normalize_images_refuses_each_breach_at_its_row_pointer() {
    let window = one_hour_window();
    let uuid_owner = [(
        "owner".to_owned(),
        Spec {
            normalizer: Normalizer::UuidOrdinal,
            after: None,
        },
    )];
    assert_eq!(
        normalize_images(&[json!({"id": 1})], &uuid_owner, Order::Set, &window),
        Err(NormalizeError::NoMatch {
            pointer: "rows[0]/owner".to_owned()
        })
    );
    assert_eq!(
        normalize_images(
            &[json!({"owner": UPPER_UUID})],
            &uuid_owner,
            Order::Set,
            &window
        ),
        Err(NormalizeError::NotUuid {
            pointer: "rows[0]/owner".to_owned()
        })
    );

    let stamp = [(
        "at".to_owned(),
        Spec {
            normalizer: Normalizer::Timestamp,
            after: None,
        },
    )];
    assert_eq!(
        normalize_images(
            &[json!({"at": "2023-11-14T22:30:00"})],
            &stamp,
            Order::Set,
            &window
        ),
        Err(NormalizeError::NotTimestamp {
            pointer: "rows[0]/at".to_owned()
        })
    );
    assert_eq!(
        normalize_images(
            &[json!({"at": "2023-11-15T00:00:00Z"})],
            &stamp,
            Order::Set,
            &window
        ),
        Err(NormalizeError::OutsideWindow {
            pointer: "rows[0]/at".to_owned()
        })
    );

    let after_created = [(
        "at".to_owned(),
        Spec {
            normalizer: Normalizer::Timestamp,
            after: Some("created".to_owned()),
        },
    )];
    assert_eq!(
        normalize_images(
            &[json!({"at": "2023-11-14T22:30:00+00:00"})],
            &after_created,
            Order::Set,
            &window
        ),
        Err(NormalizeError::AfterMissing {
            pointer: "rows[0]/at".to_owned()
        })
    );

    let sequence = [(
        "seq".to_owned(),
        Spec {
            normalizer: Normalizer::Monotonic,
            after: None,
        },
    )];
    assert_eq!(
        normalize_images(&[json!({"seq": 1})], &sequence, Order::Set, &window),
        Err(NormalizeError::MonotonicNeedsSequence)
    );
    assert_eq!(
        normalize_images(
            &[json!({"seq": 2}), json!({"seq": 2})],
            &sequence,
            Order::AsRecorded,
            &window
        ),
        Err(NormalizeError::NotMonotonic {
            pointer: "rows[1]/seq".to_owned()
        })
    );
    assert_eq!(
        normalize_images(
            &[json!({"seq": "9"}), json!({"seq": "10"})],
            &sequence,
            Order::AsRecorded,
            &window
        ),
        Ok(vec![json!({"seq": "@seq@"}), json!({"seq": "@seq@"})])
    );
}

#[test]
fn normalize_images_shares_one_uuid_counter_and_masks_an_after_timestamp() {
    let rules = [
        (
            "owner".to_owned(),
            Spec {
                normalizer: Normalizer::UuidOrdinal,
                after: None,
            },
        ),
        (
            "at".to_owned(),
            Spec {
                normalizer: Normalizer::Timestamp,
                after: Some("created".to_owned()),
            },
        ),
    ];
    let images = [
        json!({"owner": U2, "created": "2023-11-14T22:10:00+00:00", "at": "2023-11-14T22:20:00+00:00"}),
        json!({"owner": U1, "created": "2023-11-14T22:10:00+00:00", "at": "2023-11-14T22:20:00+00:00"}),
        json!({"owner": U2, "created": "2023-11-14T22:10:00+00:00", "at": "2023-11-14T22:20:00+00:00"}),
    ];
    assert_eq!(
        normalize_images(&images, &rules, Order::Set, &one_hour_window()),
        Ok(vec![
            json!({"owner": "@uuid:1@", "created": "2023-11-14T22:10:00+00:00", "at": "@ts@"}),
            json!({"owner": "@uuid:2@", "created": "2023-11-14T22:10:00+00:00", "at": "@ts@"}),
            json!({"owner": "@uuid:1@", "created": "2023-11-14T22:10:00+00:00", "at": "@ts@"}),
        ])
    );
}

#[test]
fn uuid_ordinals_take_lowercase_canonical_text_only() {
    assert!(is_uuid("aabbccdd-eeff-4011-8122-334455667788"));
    assert!(!is_uuid(UPPER_UUID));
    assert!(!is_uuid("aabbccdd-eeff-4011-8122-33445566778"));
}

#[test]
fn redact_replaces_secret_keys_at_any_depth() {
    let document = json!({"outer": {"password": "p"}, "items": [{"dsn": "d"}]});
    assert_eq!(
        redact(&document),
        json!({"outer": {"password": "@secret@"}, "items": [{"dsn": "@secret@"}]})
    );
}

#[test]
fn monotonic_is_refused_in_a_document_as_python_does() {
    // Python normalize_document: "monotonic needs a sequence at /x" (only a sequence unit may).
    let rules = [("/x".to_owned(), Normalizer::Monotonic)];
    assert_eq!(
        normalize_document(&json!({"x": 3}), &json!([]), &rules, &one_hour_window()),
        Err(NormalizeError::MonotonicInDocument {
            pointer: "/x".to_owned()
        })
    );
}

#[test]
fn a_wildcard_is_not_a_pointer_and_a_rule_that_matches_nothing_fails() {
    // Python: "normalizer pointer /b/* matches nothing" (exact RFC 6901 pointers only).
    let rules = [("/b/*".to_owned(), Normalizer::UuidOrdinal)];
    assert_eq!(
        normalize_document(&json!({"b": [U1]}), &json!([]), &rules, &one_hour_window()),
        Err(NormalizeError::NoMatch {
            pointer: "/b/*".to_owned()
        })
    );
}

#[test]
fn a_mask_applies_after_the_rules_so_a_masked_uuid_takes_an_ordinal() {
    // Python _comparison over a non-HTTP case: the rules number first (first is 1, second is 2),
    // then the mask replaces first: {"result": {"first": "@mask@", "second": "@uuid:2@"}}.
    let document = json!({"result": {"first": U1, "second": U2}});
    let rules = [
        ("/result/first".to_owned(), Normalizer::UuidOrdinal),
        ("/result/second".to_owned(), Normalizer::UuidOrdinal),
    ];
    assert_eq!(
        normalize_document(
            &document,
            &json!(["/result/first"]),
            &rules,
            &one_hour_window()
        ),
        Ok(json!({"result": {"first": "@mask@", "second": "@uuid:2@"}}))
    );
}

#[test]
fn a_masked_wrong_typed_value_is_still_a_case_error() {
    // Python validates the value before the mask: "uuid_ordinal needs a canonical UUID at /x".
    let rules = [("/x".to_owned(), Normalizer::UuidOrdinal)];
    assert_eq!(
        normalize_document(
            &json!({"x": "not-a-uuid"}),
            &json!(["/x"]),
            &rules,
            &one_hour_window()
        ),
        Err(NormalizeError::NotUuid {
            pointer: "/x".to_owned()
        })
    );
}

#[test]
fn a_none_rule_under_a_masked_container_still_matches_its_leaf() {
    // Python (non-HTTP): the rule matches the leaf and the container is masked afterwards.
    let rules = [("/body/id".to_owned(), Normalizer::NoOp)];
    assert_eq!(
        normalize_document(
            &json!({"body": {"id": U1}}),
            &json!(["/body"]),
            &rules,
            &one_hour_window()
        ),
        Ok(json!({"body": "@mask@"}))
    );
}

#[test]
fn exact_list_index_pointers_number_by_document_order() {
    // Golden: Python with its list-index token made an int (unpatched Python raises TypeError,
    // a found defect the port does not carry over).
    let document = json!({"a": {"id": U1}, "b": [U2, U1, U2]});
    let rules = [
        ("/a/id".to_owned(), Normalizer::UuidOrdinal),
        ("/b/0".to_owned(), Normalizer::UuidOrdinal),
        ("/b/1".to_owned(), Normalizer::UuidOrdinal),
        ("/b/2".to_owned(), Normalizer::UuidOrdinal),
    ];
    assert_eq!(
        normalize_document(&document, &json!([]), &rules, &one_hour_window()),
        Ok(json!({"a": {"id": "@uuid:1@"}, "b": ["@uuid:2@", "@uuid:1@", "@uuid:2@"]}))
    );
}

#[test]
fn a_none_rule_on_a_list_element_leaf_matches_that_leaf() {
    // Golden: Python (patched list index) leaves {"b": ["x", "y"]} unchanged.
    let rules = [("/b/1".to_owned(), Normalizer::NoOp)];
    assert_eq!(
        normalize_document(
            &json!({"b": ["x", "y"]}),
            &json!([]),
            &rules,
            &one_hour_window()
        ),
        Ok(json!({"b": ["x", "y"]}))
    );
}

#[test]
fn an_uppercase_uuid_in_a_document_is_refused() {
    // Python: "uuid_ordinal needs a canonical UUID at /response/id" (lowercase only).
    let rules = [("/response/id".to_owned(), Normalizer::UuidOrdinal)];
    assert_eq!(
        normalize_document(
            &json!({"response": {"id": UPPER_UUID}}),
            &json!([]),
            &rules,
            &one_hour_window()
        ),
        Err(NormalizeError::NotUuid {
            pointer: "/response/id".to_owned()
        })
    );
}

#[test]
fn a_none_or_bytes_rule_on_a_non_leaf_column_matches_nothing() {
    // Python normalize_images: "normalizer pointer rows[0]/payload matches nothing" (leaves only).
    let none = [(
        "payload".to_owned(),
        Spec {
            normalizer: Normalizer::NoOp,
            after: None,
        },
    )];
    assert_eq!(
        normalize_images(
            &[json!({"id": "a", "payload": {"x": 1}})],
            &none,
            Order::Set,
            &one_hour_window()
        ),
        Err(NormalizeError::NoMatch {
            pointer: "rows[0]/payload".to_owned()
        })
    );
    let bytes = [(
        "payload".to_owned(),
        Spec {
            normalizer: Normalizer::Bytes,
            after: None,
        },
    )];
    assert_eq!(
        normalize_images(
            &[json!({"id": "a", "payload": []})],
            &bytes,
            Order::Set,
            &one_hour_window()
        ),
        Err(NormalizeError::NoMatch {
            pointer: "rows[0]/payload".to_owned()
        })
    );
}

#[test]
fn a_none_rule_on_a_db_leaf_leaves_the_row_unchanged() {
    // Python normalize_rows: {"id": "a", "payload": "x"} is unchanged.
    let none = [(
        "payload".to_owned(),
        Spec {
            normalizer: Normalizer::NoOp,
            after: None,
        },
    )];
    assert_eq!(
        normalize_images(
            &[json!({"id": "a", "payload": "x"})],
            &none,
            Order::Set,
            &one_hour_window()
        ),
        Ok(vec![json!({"id": "a", "payload": "x"})])
    );
}

#[test]
fn document_keys_are_numbered_in_insertion_order_as_python_dicts_are() {
    // Python reads keys in JSON text order, so b is @uuid:1@ and a is @uuid:2@. This holds only
    // because serde_json keeps insertion order (preserve_order).
    let document = json!({"b": U1, "a": U2});
    let rules = [
        ("/a".to_owned(), Normalizer::UuidOrdinal),
        ("/b".to_owned(), Normalizer::UuidOrdinal),
    ];
    assert_eq!(
        normalize_document(&document, &json!([]), &rules, &one_hour_window()),
        Ok(json!({"b": "@uuid:1@", "a": "@uuid:2@"}))
    );
}
