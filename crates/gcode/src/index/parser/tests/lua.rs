use super::common::{parse_lua, parse_source};

macro_rules! assert_lua_local_import {
    ($parsed:expr, $callee_name:expr, $expected_file:expr) => {{
        let callee_name: &str = $callee_name;
        let found = $parsed.calls.iter().any(|call| {
            call.callee_target_kind.as_str() == "local_import"
                && call.callee_name == callee_name
                && call
                    .local_import_candidate_files()
                    .iter()
                    .any(|file| file == $expected_file)
        });
        assert!(
            found,
            "no local_import call for {callee_name} carried candidate file {}; calls: {:?}",
            $expected_file, $parsed.calls
        );
    }};
}

#[test]
fn extracts_lua_function_symbols_and_calls() {
    let parsed = parse_lua(
        r#"
local M = {}

function greet()
  helper()
end

local function helper()
end

function M.build()
end

function M:run()
end

M.factory = function()
end

return M
"#,
        &[],
    );

    for (name, kind) in [
        ("greet", "function"),
        ("helper", "function"),
        ("build", "function"),
        ("run", "method"),
        ("factory", "function"),
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
    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "helper")
        .expect("helper call");
    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_eq!(call.callee_symbol_id, Some(helper_id));
}

#[test]
fn resolves_lua_require_alias_member_and_bare_calls() {
    let parsed = parse_lua(
        r#"
local widgets = require("app.widgets")
local make = require("app.widgets").make
local app = require("app")

function run()
  widgets.build()
  widgets:draw()
  make()
  app.start()
  require("app.widgets").build()
end
"#,
        &[
            (
                "lua/app/widgets.lua",
                r#"
local M = {}
function M.build() end
function M:draw() end
function M.make() end
return M
"#,
            ),
            (
                "lua/app/init.lua",
                r#"
local M = {}
function M.start() end
return M
"#,
            ),
        ],
    );

    assert!(
        parsed
            .imports
            .iter()
            .any(|import| import.module_name == "app.widgets")
    );
    assert!(
        parsed
            .imports
            .iter()
            .any(|import| import.module_name == "app")
    );

    assert_lua_local_import!(&parsed, "build", "lua/app/widgets.lua");
    assert_lua_local_import!(&parsed, "draw", "lua/app/widgets.lua");
    assert_lua_local_import!(&parsed, "make", "lua/app/widgets.lua");
    assert_lua_local_import!(&parsed, "start", "lua/app/init.lua");
    let build_calls = parsed
        .calls
        .iter()
        .filter(|call| {
            call.callee_name == "build" && call.callee_target_kind.as_str() == "local_import"
        })
        .count();
    assert_eq!(build_calls, 2, "calls: {:?}", parsed.calls);
}

#[test]
fn leaves_external_lua_require_members_unresolved() {
    let parsed = parse_lua(
        r#"
local json = require("json")

function run()
  json.encode({})
end
"#,
        &[],
    );

    assert!(
        parsed
            .imports
            .iter()
            .any(|import| import.module_name == "json")
    );
    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "encode")
        .expect("encode call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
}

#[test]
fn detects_lua_extension() {
    let parsed = parse_source(
        "lua/plugin/init.lua",
        r#"
local M = {}
function M.setup() end
return M
"#,
        &[],
    );

    assert!(
        parsed
            .symbols
            .iter()
            .any(|symbol| symbol.name == "setup" && symbol.kind == "function")
    );
}
