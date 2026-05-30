use super::super::{call_qualifier_path, line_terminator_len, split_qualified_callee};
use super::common::parse_python;

#[test]
fn line_terminator_len_tracks_lf_crlf_and_eof() {
    let text = "import 'a';\r\nhttp.Client();\nlast()";
    assert_eq!(line_terminator_len(text, 0, "import 'a';".len()), 2);

    let second_start = "import 'a';\r\n".len();
    assert_eq!(
        line_terminator_len(text, second_start, "http.Client();".len()),
        1
    );

    let last_start = "import 'a';\r\nhttp.Client();\n".len();
    assert_eq!(line_terminator_len(text, last_start, "last()".len()), 0);
}

#[test]
fn explicit_qualified_raw_callee_takes_precedence_over_member_prefix() {
    let mut inferred_called = false;
    let (_, qualifier_from_name) = split_qualified_callee("Vendor\\Pkg\\helper");

    let qualifier_path = call_qualifier_path(qualifier_from_name, || {
        inferred_called = true;
        Some("Vendor".to_string())
    });

    assert_eq!(qualifier_path.as_deref(), Some("Vendor\\Pkg"));
    assert!(!inferred_called);
}

#[test]
fn resolves_unique_same_file_bare_calls() {
    let parsed = parse_python(
        r#"
def foo():
    pass

def bar():
    foo()
"#,
    );

    let foo = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "foo")
        .expect("foo symbol");
    let bar = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "bar")
        .expect("bar symbol");
    let call = parsed.calls.first().expect("call");

    assert_eq!(call.caller_symbol_id, bar.id);
    assert_eq!(call.callee_symbol_id.as_deref(), Some(foo.id.as_str()));
}

#[test]
fn resolves_same_class_member_calls() {
    let parsed = parse_python(
        r#"
class Greeter:
    def greet(self):
        self.render()

    def render(self):
        pass
"#,
    );

    let render = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.qualified_name == "Greeter.render")
        .expect("render method");
    let call = parsed.calls.first().expect("call");

    assert_eq!(call.callee_symbol_id.as_deref(), Some(render.id.as_str()));
}

#[test]
fn leaves_ambiguous_bare_calls_unresolved() {
    let parsed = parse_python(
        r#"
def foo():
    pass

class A:
    def foo(self):
        pass

def bar():
    foo()
"#,
    );

    let call = parsed.calls.first().expect("call");
    assert!(call.callee_symbol_id.is_none());
}

#[test]
fn leaves_non_local_member_calls_unresolved() {
    let parsed = parse_python(
        r#"
class A:
    def bar(self):
        obj.render()

class B:
    def render(self):
        pass
"#,
    );

    let call = parsed.calls.first().expect("call");
    assert!(call.callee_symbol_id.is_none());
}
