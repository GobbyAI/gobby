//! Internal MCP family contract (plan `rust-internal-mcp-servers` section 1.1).
//!
//! Tests sit in `mod mcp_contract` so `cargo nextest run -p gobby-core
//! -E 'test(mcp_contract)'` selects them by name.

mod mcp_contract {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use gobby_core::mcp::{
        RegistryError, ToolContext, ToolDefinition, ToolError, ToolFamily, ToolRegistry,
        ToolServer, canonical_definition_json, definition_hash,
    };
    use schemars::JsonSchema;
    use serde::{Deserialize, Deserializer};
    use serde_json::{Value, json};

    /// A family crate's view: a static server descriptor built from closures.
    struct Family(fn() -> ToolServer);

    impl ToolFamily for Family {
        fn server(&self) -> ToolServer {
            (self.0)()
        }
    }

    fn registry(families: Vec<fn() -> ToolServer>) -> Result<ToolRegistry, RegistryError> {
        ToolRegistry::new(
            families
                .into_iter()
                .map(|f| Box::new(Family(f)) as Box<dyn ToolFamily>)
                .collect(),
        )
    }

    #[derive(Deserialize, JsonSchema)]
    struct Empty {}

    fn noop(name: &'static str) -> ToolDefinition {
        ToolDefinition::new(
            name,
            "Does nothing.",
            |_ctx: ToolContext, _req: Empty| async { Ok(Value::Null) },
        )
    }

    // 1.1.2 ---------------------------------------------------------------

    static DESERIALIZE_CALLS: AtomicUsize = AtomicUsize::new(0);

    fn counted_string<'de, D: Deserializer<'de>>(d: D) -> Result<String, D::Error> {
        DESERIALIZE_CALLS.fetch_add(1, Ordering::SeqCst);
        String::deserialize(d)
    }

    #[derive(Deserialize, JsonSchema)]
    struct EchoRequest {
        /// Text to echo.
        #[serde(deserialize_with = "counted_string")]
        message: String,
    }

    #[tokio::test]
    async fn derived_schema_validates_before_dispatch() {
        let handled = Arc::new(AtomicUsize::new(0));
        let seen = handled.clone();
        let family = move || {
            let seen = seen.clone();
            ToolServer::new(
                "gobby-echo",
                "Echo server.",
                vec![ToolDefinition::new(
                    "echo",
                    "Echo a message back.",
                    move |ctx: ToolContext, req: EchoRequest| {
                        let seen = seen.clone();
                        async move {
                            seen.fetch_add(1, Ordering::SeqCst);
                            Ok(json!({"message": req.message, "caller": ctx.session_id}))
                        }
                    },
                )],
            )
        };
        // Closures that capture state cannot coerce to `fn`; wrap them directly.
        struct Capturing<F>(F);
        impl<F: Fn() -> ToolServer + Send + Sync> ToolFamily for Capturing<F> {
            fn server(&self) -> ToolServer {
                (self.0)()
            }
        }
        let registry = ToolRegistry::new(vec![Box::new(Capturing(family))]).expect("registry");

        // Discovery and validation share one normalized schema: no `$schema` or
        // `title`, and no caller-context fields.
        let tool = registry
            .tool("gobby-echo", "echo")
            .expect("registered tool");
        assert_eq!(
            tool.input_schema(),
            &json!({
                "type": "object",
                "properties": {"message": {"type": "string", "description": "Text to echo."}},
                "required": ["message"],
            })
        );

        let ctx = ToolContext {
            session_id: Some("caller-session".into()),
            ..ToolContext::default()
        };
        for malformed in [json!({"message": 5}), json!({}), json!("not an object")] {
            let err = registry
                .call("gobby-echo", "echo", ctx.clone(), malformed.clone())
                .await
                .expect_err("malformed arguments must fail");
            assert!(
                matches!(err, ToolError::InvalidArguments(_)),
                "{malformed}: expected InvalidArguments, got {err:?}"
            );
        }
        assert_eq!(
            DESERIALIZE_CALLS.load(Ordering::SeqCst),
            0,
            "schema validation must reject before deserialization"
        );
        assert_eq!(
            handled.load(Ordering::SeqCst),
            0,
            "handler ran on bad input"
        );

        let ok = registry
            .call("gobby-echo", "echo", ctx, json!({"message": "hi"}))
            .await
            .expect("valid call");
        assert_eq!(ok, json!({"message": "hi", "caller": "caller-session"}));
        assert_eq!(handled.load(Ordering::SeqCst), 1);
    }

    // 1.1.3 ---------------------------------------------------------------

    /// Generated with `gobby.mcp_proxy.schema_hash.compute_schema_hash(schema,
    /// description)`; Python's `None` description hashes as `""`.
    fn python_vectors() -> Vec<(&'static str, &'static str, Value, &'static str)> {
        vec![
            (
                "empty_description_null_schema",
                "",
                Value::Null,
                "e9f76ed3dc84fb4d",
            ),
            (
                "simple_object",
                "Echo a message back.",
                json!({"type": "object", "properties": {"message": {"type": "string"}},
                       "required": ["message"]}),
                "d8384b6c28802a82",
            ),
            (
                "nested_unsorted_keys",
                "List tasks \u{2014} filtered by status.",
                json!({"required": ["status"], "type": "object", "properties": {
                    "status": {"enum": ["open", "closed"], "type": "string",
                               "description": "Task state"},
                    "limit": {"type": "integer", "default": 50, "minimum": 1}}}),
                "d92e90268faaec0c",
            ),
            (
                "non_ascii_and_astral",
                "Caf\u{e9} na\u{ef}ve r\u{e9}sum\u{e9} \u{1F680} \"quoted\" back\\slash\nnewline",
                json!({"type": "object", "properties": {"emoji": {"type": "string",
                       "description": "\u{1F680} \u{fc}n\u{ef}c\u{f6}d\u{e9}"}}}),
                "f1ed5a5284dc8621",
            ),
            (
                "numbers_bools_nulls",
                "",
                json!({"type": "object", "properties": {
                    "ratio": {"type": "number", "default": 0.5, "maximum": 1.0},
                    "flag": {"type": "boolean", "default": false},
                    "x": {"default": null}}, "additionalProperties": false}),
                "0d25c67a2a609bb2",
            ),
        ]
    }

    #[test]
    fn definition_hash_matches_python_vectors() {
        for (name, description, schema, expected) in python_vectors() {
            assert_eq!(
                definition_hash(description, &schema),
                expected,
                "vector {name}"
            );
        }

        // Python `json.dumps` defaults to ensure_ascii: non-ASCII becomes
        // `\uXXXX`, astral characters become surrogate pairs.
        let (_, description, schema, _) = python_vectors().swap_remove(3);
        assert_eq!(
            canonical_definition_json(description, &schema),
            r#"{"description":"Café naïve résumé 🚀 \"quoted\" back\\slash\nnewline","input_schema":{"properties":{"emoji":{"description":"🚀 ünïcödé","type":"string"}},"type":"object"}}"#
        );

        // Key order never changes the hash.
        let reordered: Value = serde_json::from_str(
            r#"{"properties":{"message":{"type":"string"}},"required":["message"],"type":"object"}"#,
        )
        .unwrap();
        assert_eq!(
            definition_hash("Echo a message back.", &reordered),
            "d8384b6c28802a82"
        );
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct ListRequest {
        status: String,
    }

    #[derive(JsonSchema)]
    #[allow(dead_code)]
    struct ListResult {
        count: u32,
    }

    fn list_tool() -> ToolDefinition {
        ToolDefinition::new(
            "list",
            "List things.",
            |_ctx: ToolContext, _req: ListRequest| async { Ok(Value::Null) },
        )
    }

    #[test]
    fn registered_hash_covers_only_description_and_input_schema() {
        let registry = registry(vec![
            || ToolServer::new("gobby-plain", "Plain.", vec![list_tool()]),
            || {
                ToolServer::new(
                    "gobby-decorated",
                    "Decorated.",
                    vec![
                        list_tool()
                            .brief("List.")
                            .output_schema::<ListResult>()
                            .read_only(true),
                    ],
                )
            },
        ])
        .expect("registry");

        let plain = registry.tool("gobby-plain", "list").expect("plain");
        let decorated = registry.tool("gobby-decorated", "list").expect("decorated");
        assert_eq!(
            plain.definition_hash(),
            definition_hash(plain.description(), plain.input_schema())
        );
        assert_eq!(plain.definition_hash(), decorated.definition_hash());

        // 1.1.1: descriptor metadata survives registration.
        assert_eq!(plain.brief(), None);
        assert!(!plain.read_only());
        assert!(plain.output_schema().is_none());
        assert_eq!(decorated.brief(), Some("List."));
        assert!(decorated.read_only());
        let output = decorated.output_schema().expect("output schema");
        assert_eq!(output["properties"]["count"]["type"], json!("integer"));
    }

    // 1.1.4 ---------------------------------------------------------------

    /// Stands in for any request field that leaks the caller context into the
    /// public schema; the real `ToolContext` has no `JsonSchema` impl.
    #[allow(dead_code)]
    mod leaky {
        #[derive(serde::Deserialize, schemars::JsonSchema)]
        pub struct ToolContext {
            pub session_id: String,
        }
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct LeakyRequest {
        context: leaky::ToolContext,
    }

    #[test]
    fn invalid_descriptors_fail_startup() {
        // Two families claim one server name.
        assert_eq!(
            registry(vec![
                || ToolServer::new("gobby-dup", "A.", vec![noop("a")]),
                || ToolServer::new("gobby-dup", "B.", vec![noop("b")]),
            ])
            .err(),
            Some(RegistryError::DuplicateServer {
                server: "gobby-dup".into()
            })
        );

        // One server lists a tool name twice.
        assert_eq!(
            registry(vec![|| {
                ToolServer::new("gobby-dup", "A.", vec![noop("same"), noop("same")])
            }])
            .err(),
            Some(RegistryError::DuplicateTool {
                server: "gobby-dup".into(),
                tool: "same".into()
            })
        );

        // Public names must be non-empty and contain no whitespace or slashes.
        let bad_server: Vec<fn() -> ToolServer> =
            vec![|| ToolServer::new("", "Empty.", vec![noop("ok")]), || {
                ToolServer::new("gobby tasks", "Space.", vec![noop("ok")])
            }];
        for family in bad_server {
            assert!(
                matches!(
                    registry(vec![family]).err(),
                    Some(RegistryError::InvalidName { .. })
                ),
                "invalid server name accepted"
            );
        }
        let bad_tool: Vec<fn() -> ToolServer> = vec![
            || ToolServer::new("gobby-names", "Names.", vec![noop("")]),
            || ToolServer::new("gobby-names", "Names.", vec![noop("list tasks")]),
            || ToolServer::new("gobby-names", "Names.", vec![noop("list/tasks")]),
        ];
        for family in bad_tool {
            assert!(
                matches!(
                    registry(vec![family]).err(),
                    Some(RegistryError::InvalidName { .. })
                ),
                "invalid tool name accepted"
            );
        }

        // A request schema that exposes ToolContext.
        assert_eq!(
            registry(vec![|| {
                ToolServer::new(
                    "gobby-leaky",
                    "Leaky.",
                    vec![ToolDefinition::new(
                        "leak",
                        "Leaks caller context.",
                        |_ctx: ToolContext, _req: LeakyRequest| async { Ok(Value::Null) },
                    )],
                )
            }])
            .err(),
            Some(RegistryError::ContextInSchema {
                server: "gobby-leaky".into(),
                tool: "leak".into()
            })
        );

        // A valid descriptor still registers.
        assert!(
            registry(vec![|| ToolServer::new(
                "gobby-ok",
                "Ok.",
                vec![noop("list_tasks")]
            )])
            .is_ok()
        );
    }
}
