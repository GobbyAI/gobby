use crate::models::{CallRelation, CallTargetKind};

use super::common::{parse_objc, parse_objcxx, parse_source};

macro_rules! assert_local_import {
    ($parsed:expr, $callee:expr, $file:expr) => {{
        let call = $parsed
            .calls
            .iter()
            .find(|call| {
                call.callee_name == $callee
                    && call.callee_target_kind == CallTargetKind::LocalImport
            })
            .unwrap_or_else(|| {
                panic!(
                    "missing local import call `{}`: {:?}",
                    $callee, $parsed.calls
                )
            });
        assert_eq!(call.local_import_candidate_files(), vec![$file.to_string()]);
    }};
}

#[test]
fn extracts_objc_symbols_and_same_file_calls() {
    let parsed = parse_objc(
        r#"
#import "Widget.h"

@interface Widget : NSObject
- (void)render;
+ (instancetype)make;
@end

static void helper(void) {}

@implementation Widget
- (void)render {
  helper();
}

+ (instancetype)make {
  return [Widget new];
}
@end
"#,
        &[],
    );

    for (name, kind) in [
        ("Widget", "class"),
        ("render", "method"),
        ("make", "method"),
        ("helper", "function"),
    ] {
        assert!(
            parsed
                .symbols
                .iter()
                .any(|symbol| symbol.name == name && symbol.kind == kind),
            "missing {kind} symbol {name}: {:?}",
            parsed.symbols
        );
    }

    let helper_id = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "helper")
        .expect("helper symbol")
        .id
        .clone();
    let helper_call = call_named(&parsed.calls, "helper");
    assert_eq!(helper_call.callee_target_kind, CallTargetKind::Symbol);
    assert_eq!(helper_call.callee_symbol_id, Some(helper_id));

    assert!(
        parsed.calls.iter().any(|call| call.callee_name == "new"),
        "missing Objective-C message send: {:?}",
        parsed.calls
    );
}

#[test]
fn resolves_objc_imported_header_message_and_c_function_calls() {
    let parsed = parse_objc(
        r#"
#import "Widget.h"
#import "Helpers.h"

void run(void) {
  Widget *widget = [Widget new];
  [widget render];
  WidgetRender(widget);
}
"#,
        &[
            (
                "Sources/App/Widget.h",
                r#"
@interface Widget : NSObject
- (void)render;
@end
"#,
            ),
            (
                "Sources/App/Helpers.h",
                r#"
void WidgetRender(Widget *widget);
"#,
            ),
        ],
    );

    assert!(parsed.imports.iter().any(|import| {
        import.file_path == "Sources/App/Sample.m" && import.module_name == "Widget.h"
    }));
    assert!(parsed.imports.iter().any(|import| {
        import.file_path == "Sources/App/Sample.m" && import.module_name == "Helpers.h"
    }));

    assert_local_import!(&parsed, "render", "Sources/App/Widget.h");
    assert_local_import!(&parsed, "WidgetRender", "Sources/App/Helpers.h");
}

#[test]
fn objcxx_files_use_objc_grammar_for_messages() {
    let parsed = parse_objcxx(
        r#"
@interface Widget
- (void)render;
@end

void run() {
  auto widget = [Widget new];
  [Widget render];
}
"#,
        &[],
    );

    assert!(
        parsed
            .symbols
            .iter()
            .any(|symbol| symbol.name == "Widget" && symbol.kind == "class"),
        "missing Obj-C++ class symbol: {:?}",
        parsed.symbols
    );
    assert!(
        parsed.calls.iter().any(|call| call.callee_name == "render"),
        "missing Obj-C++ message call: {:?}",
        parsed.calls
    );
}

#[test]
fn objc_header_uses_objc_grammar_for_symbols() {
    let parsed = parse_source(
        "Sources/App/Widget.h",
        r#"
@interface Widget
- (void)render;
@end
"#,
        &[],
    );

    assert!(
        parsed
            .symbols
            .iter()
            .any(|symbol| symbol.name == "Widget" && symbol.kind == "class"),
        "missing Objective-C header class symbol: {:?}",
        parsed.symbols
    );
    assert!(
        parsed
            .symbols
            .iter()
            .any(|symbol| symbol.name == "render" && symbol.kind == "method"),
        "missing Objective-C header method symbol: {:?}",
        parsed.symbols
    );
}

#[test]
fn cpp_header_uses_cpp_grammar_for_symbols() {
    let parsed = parse_source(
        "include/Widget.h",
        r#"
namespace app {
template <typename T>
class Box {};

class Widget {
public:
  void render();
};
}
"#,
        &[],
    );

    for name in ["Box", "Widget"] {
        assert!(
            parsed
                .symbols
                .iter()
                .any(|symbol| symbol.name == name && symbol.kind == "class"),
            "missing C++ header class symbol {name}: {:?}",
            parsed.symbols
        );
    }
}

fn call_named<'a>(calls: &'a [CallRelation], name: &str) -> &'a CallRelation {
    calls
        .iter()
        .find(|call| call.callee_name == name)
        .unwrap_or_else(|| panic!("missing call `{name}`: {calls:?}"))
}
