use std::collections::HashSet;

use crate::index::import_resolution::ImportResolutionContext;

use super::ImportIdentity;

pub(crate) fn identity_from(visible: &[&str], rows: &[(&str, &str)]) -> ImportIdentity {
    let visible = visible
        .iter()
        .map(|path| (*path).to_string())
        .collect::<HashSet<_>>();
    let imports = rows
        .iter()
        .map(|(source, module)| ((*source).to_string(), (*module).to_string()))
        .collect::<Vec<_>>();
    ImportIdentity::from_resolution(&visible, &ImportResolutionContext::default(), &imports)
}

#[test]
fn relative_imports_in_different_packages_keep_their_local_edges() {
    let rows = [
        ("src/alpha/service.py".to_string(), ".models".to_string()),
        ("src/beta/service.py".to_string(), ".models".to_string()),
    ];
    let visible = [
        "src/alpha/service.py",
        "src/alpha/models.py",
        "src/beta/service.py",
        "src/beta/models.py",
    ]
    .map(str::to_string)
    .into_iter()
    .collect();
    let identity =
        ImportIdentity::from_resolution(&visible, &ImportResolutionContext::default(), &rows);
    assert_eq!(
        identity.providers_for(".models"),
        vec!["src/alpha/models.py", "src/beta/models.py"]
    );
    assert_eq!(identity.unique_provider(".models"), None);
    let partition =
        crate::communities::partition::build_partition(&identity, &rows).expect("valid imports");
    let edges = partition
        .directed
        .iter()
        .map(|edge| (edge.importer.as_str(), edge.provider.as_str()))
        .collect::<Vec<_>>();
    assert_eq!(
        edges,
        vec![
            ("src/alpha/service.py", "src/alpha/models.py"),
            ("src/beta/service.py", "src/beta/models.py"),
        ]
    );
}

#[test]
fn missing_relative_import_does_not_borrow_another_packages_provider() {
    let rows = [
        ("src/alpha/service.py".to_string(), ".models".to_string()),
        ("src/beta/service.py".to_string(), ".models".to_string()),
    ];
    let visible = [
        "src/alpha/service.py",
        "src/alpha/models.py",
        "src/beta/service.py",
    ]
    .map(str::to_string)
    .into_iter()
    .collect();
    let identity =
        ImportIdentity::from_resolution(&visible, &ImportResolutionContext::default(), &rows);
    let partition =
        crate::communities::partition::build_partition(&identity, &rows).expect("valid imports");
    assert_eq!(partition.directed.len(), 1);
    assert_eq!(partition.directed[0].importer, "src/alpha/service.py");
    assert_eq!(partition.directed[0].provider, "src/alpha/models.py");
    assert_eq!(
        identity.unique_provider_for("src/beta/service.py", ".models"),
        None
    );
}

#[test]
fn mcg_identity_build_handles_twenty_thousand_rows() {
    const FILES: usize = 2_000;
    let visible = (0..FILES)
        .map(|index| format!("src/m{index}.py"))
        .collect::<HashSet<_>>();
    let mut imports = Vec::with_capacity(FILES * 10);
    for index in 0..FILES {
        for offset in 1..=10 {
            imports.push((
                format!("src/m{index}.py"),
                format!("m{}", (index + offset) % FILES),
            ));
        }
    }
    let started = std::time::Instant::now();
    let identity =
        ImportIdentity::from_resolution(&visible, &ImportResolutionContext::default(), &imports);
    let elapsed = started.elapsed();
    assert_eq!(identity.aliases["src/m0.py"], vec!["m0", "src.m0"]);
    assert_eq!(identity.providers["m1999"], vec!["src/m1999.py"]);
    assert_eq!(identity.aliases.len(), FILES);
    assert!(
        elapsed < std::time::Duration::from_secs(30),
        "identity build took {elapsed:?}; the one-pass build must stay linear"
    );
}
