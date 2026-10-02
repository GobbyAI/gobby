use super::common::parse_python;

#[test]
fn attribute_ranges_parameters_shadow_external_imports() {
    for (import, parameters, invocation) in [
        ("from requests import get as fetch", "fetch", "fetch()"),
        ("import requests as client", "client", "client.get()"),
    ] {
        let source =
            format!("{import}\n@decorate(flag=True)\ndef run({parameters}):\n    {invocation}\n");
        let parsed = parse_python(&source, &[]);
        let run = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.name == "run")
            .expect("run");
        let call = parsed
            .calls
            .iter()
            .max_by_key(|call| call.line)
            .expect("body call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved", "{source}");
        assert!(call.callee_external_module.is_none());
        assert_eq!(call.caller_symbol_id, run.id);
        assert_eq!(run.byte_start, source.find("@decorate").expect("decorator"));
    }
}

#[test]
fn attribute_ranges_metadata_arguments_do_not_shadow_external_imports() {
    for (import, metadata, invocation) in [
        (
            "from requests import get as fetch",
            "@decorate(fetch)",
            "fetch()",
        ),
        (
            "import requests as client",
            "@decorate(client)",
            "client.get()",
        ),
        (
            "from requests import get as fetch",
            "@decorate(\n    fetch=True,\n)",
            "fetch()",
        ),
    ] {
        let source = format!("{import}\n{metadata}\ndef run():\n    {invocation}\n");
        let parsed = parse_python(&source, &[]);
        let run = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.name == "run")
            .expect("run");
        let call = parsed
            .calls
            .iter()
            .max_by_key(|call| call.line)
            .expect("body call");
        assert_eq!(call.callee_target_kind.as_str(), "external", "{source}");
        assert_eq!(call.callee_external_module.as_deref(), Some("requests"));
        assert_eq!(call.callee_name, "get");
        assert_eq!(call.caller_symbol_id, run.id);
        assert_eq!(run.byte_start, source.find("@decorate").expect("decorator"));
    }
}

#[test]
fn attribute_ranges_parameters_shadow_local_imports() {
    let source =
        "from helpers import get as fetch\n@decorate(flag=True)\ndef run(fetch):\n    fetch()\n";
    let parsed = parse_python(source, &[("helpers.py", "def get():\n    pass\n")]);
    let run = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "run")
        .expect("run");
    let call = parsed
        .calls
        .iter()
        .max_by_key(|call| call.line)
        .expect("body call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    assert_eq!(call.caller_symbol_id, run.id);
    assert_eq!(run.byte_start, source.find("@decorate").expect("decorator"));
}

#[test]
fn attribute_ranges_metadata_arguments_do_not_shadow_local_imports() {
    for metadata in ["@decorate(fetch)", "@decorate(\n    fetch=True,\n)"] {
        let source =
            format!("from helpers import get as fetch\n{metadata}\ndef run():\n    fetch()\n");
        let parsed = parse_python(&source, &[("helpers.py", "def get():\n    pass\n")]);
        let run = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.name == "run")
            .expect("run");
        let call = parsed
            .calls
            .iter()
            .max_by_key(|call| call.line)
            .expect("body call");
        assert_eq!(call.callee_target_kind.as_str(), "local_import", "{source}");
        assert_eq!(call.callee_name, "get");
        assert_eq!(call.caller_symbol_id, run.id);
        assert_eq!(run.byte_start, source.find("@decorate").expect("decorator"));
    }
}

#[test]
fn attribute_ranges_metadata_calls_use_outer_callable_scope() {
    let source = "from requests import get as fetch\ndef outer(fetch):\n    @decorate(flag=fetch())\n    def inner():\n        pass\n";
    let parsed = parse_python(source, &[]);
    let outer = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "outer")
        .expect("outer");
    let inner = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "inner")
        .expect("inner");
    let fetch = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "fetch" || call.callee_name == "get")
        .expect("metadata fetch call");
    assert_eq!(fetch.callee_target_kind.as_str(), "unresolved");
    assert_eq!(fetch.caller_symbol_id, outer.id);
    assert_eq!(
        inner.byte_start,
        source.find("@decorate").expect("decorator")
    );
}

#[test]
fn attribute_ranges_metadata_calls_use_module_bindings() {
    let source = "from requests import get as fetch\nfetch = replacement\n@decorate(flag=fetch())\ndef run(fetch):\n    pass\n";
    let parsed = parse_python(source, &[]);
    let run = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "run")
        .expect("run");
    let fetch = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "fetch" || call.callee_name == "get")
        .expect("metadata fetch call");
    assert_eq!(fetch.callee_target_kind.as_str(), "unresolved");
    assert!(fetch.caller_symbol_id.is_empty());
    assert_eq!(run.byte_start, source.find("@decorate").expect("decorator"));
}
