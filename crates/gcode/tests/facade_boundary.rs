use syn::{Item, UseTree, Visibility};

fn public_use_names(tree: &UseTree, names: &mut Vec<String>) {
    match tree {
        UseTree::Path(path) => public_use_names(&path.tree, names),
        UseTree::Name(name) => names.push(format!("use {}", name.ident)),
        UseTree::Rename(rename) => names.push(format!("use {}", rename.rename)),
        UseTree::Group(group) => {
            for item in &group.items {
                public_use_names(item, names);
            }
        }
        UseTree::Glob(_) => names.push("use *".to_string()),
    }
}

#[test]
fn public_surface_is_pinned() {
    let syntax = syn::parse_file(include_str!("../src/lib.rs")).expect("parse library root");
    let mut actual = Vec::new();

    for item in syntax.items {
        match item {
            Item::Fn(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("fn {}", item.sig.ident));
            }
            Item::Mod(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("mod {}", item.ident));
            }
            Item::Use(item) if matches!(item.vis, Visibility::Public(_)) => {
                public_use_names(&item.tree, &mut actual);
            }
            Item::Const(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("const {}", item.ident));
            }
            Item::Enum(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("enum {}", item.ident));
            }
            Item::ExternCrate(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("extern crate {}", item.ident));
            }
            Item::Static(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("static {}", item.ident));
            }
            Item::Struct(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("struct {}", item.ident));
            }
            Item::Trait(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("trait {}", item.ident));
            }
            Item::TraitAlias(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("trait alias {}", item.ident));
            }
            Item::Type(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("type {}", item.ident));
            }
            Item::Union(item) if matches!(item.vis, Visibility::Public(_)) => {
                actual.push(format!("union {}", item.ident));
            }
            _ => {}
        }
    }

    actual.sort();
    let mut expected = vec![
        "fn run_cli",
        "mod codewiki_facts",
        "mod commands",
        "mod contract",
        "mod test_env",
        "use CodeFactWriteRequest",
        "use CodeFactWriteSummary",
        "use CodeSymbolVectorPayload",
        "use CodeSymbolVectorSearchHit",
        "use CodeSymbolVectorSearchRequest",
        "use GraphLifecycleOutput",
        "use GraphLifecycleRequest",
        "use GraphReadRequest",
        "use IndexDegradation",
        "use IndexDurations",
        "use IndexOutcome",
        "use IndexRequest",
        "use ProjectionSyncRequest",
        "use ProjectionSyncStatus",
    ];
    expected.sort();
    assert_eq!(actual, expected);

    let _run_cli: fn() -> std::process::ExitCode = gobby_code::run_cli;
    fn assert_public<T>() {}
    assert_public::<gobby_code::CodeFactWriteRequest>();
    assert_public::<gobby_code::CodeFactWriteSummary>();
    assert_public::<gobby_code::GraphLifecycleOutput>();
    assert_public::<gobby_code::GraphLifecycleRequest>();
    assert_public::<gobby_code::GraphReadRequest>();
    assert_public::<gobby_code::CodeSymbolVectorPayload>();
    assert_public::<gobby_code::CodeSymbolVectorSearchHit>();
    assert_public::<gobby_code::CodeSymbolVectorSearchRequest>();
    assert_public::<gobby_code::IndexDegradation>();
    assert_public::<gobby_code::IndexDurations>();
    assert_public::<gobby_code::IndexOutcome>();
    assert_public::<gobby_code::IndexRequest>();
    assert_public::<gobby_code::ProjectionSyncRequest>();
    assert_public::<gobby_code::ProjectionSyncStatus>();
}
