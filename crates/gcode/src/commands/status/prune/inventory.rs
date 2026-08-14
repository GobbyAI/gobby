//! Classification of projection records against the indexed-project authority.

#![allow(dead_code)]

use std::collections::HashSet;

use crate::config;

#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct CollectionInventory {
    pub(super) scanned: usize,
    pub(super) active: usize,
    pub(super) invalid: usize,
    pub(super) existing_orphan_ids: Vec<String>,
    pub(super) would_be_orphan_ids: Vec<String>,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct ScopeInventory {
    pub(super) scanned: usize,
    pub(super) active: usize,
    pub(super) invalid: usize,
    pub(super) existing_orphan_ids: Vec<String>,
    pub(super) would_be_orphan_ids: Vec<String>,
}

pub(super) fn classify_collection_inventory(
    collections: &[String],
    authority: &HashSet<String>,
    stale_project_ids: &HashSet<String>,
) -> CollectionInventory {
    let mut inventory = CollectionInventory::default();
    for collection in collections {
        let Some(project_id) = collection.strip_prefix(config::CODE_SYMBOL_COLLECTION_PREFIX)
        else {
            continue;
        };
        inventory.scanned += 1;

        let Ok(parsed) = uuid::Uuid::parse_str(project_id) else {
            inventory.invalid += 1;
            continue;
        };
        if parsed.to_string() != project_id {
            inventory.invalid += 1;
            continue;
        }

        if stale_project_ids.contains(project_id) {
            inventory.would_be_orphan_ids.push(project_id.to_string());
        } else if authority.contains(project_id) {
            inventory.active += 1;
        } else {
            inventory.existing_orphan_ids.push(project_id.to_string());
        }
    }
    inventory
}

pub(super) fn classify_scope_inventory(
    project_ids: &[String],
    authority: &HashSet<String>,
    stale_project_ids: &HashSet<String>,
) -> ScopeInventory {
    let mut inventory = ScopeInventory::default();
    for project_id in project_ids {
        inventory.scanned += 1;
        let Ok(parsed) = uuid::Uuid::parse_str(project_id) else {
            inventory.invalid += 1;
            continue;
        };
        if parsed.to_string() != *project_id {
            inventory.invalid += 1;
            continue;
        }
        if stale_project_ids.contains(project_id) {
            inventory.would_be_orphan_ids.push(project_id.clone());
        } else if authority.contains(project_id) {
            inventory.active += 1;
        } else {
            inventory.existing_orphan_ids.push(project_id.clone());
        }
    }
    inventory
}

pub(super) fn all_collection_orphan_ids(inventory: &CollectionInventory) -> Vec<String> {
    sorted_union(
        &inventory.existing_orphan_ids,
        &inventory.would_be_orphan_ids,
    )
}

pub(super) fn all_scope_orphan_ids(inventory: &ScopeInventory) -> Vec<String> {
    sorted_union(
        &inventory.existing_orphan_ids,
        &inventory.would_be_orphan_ids,
    )
}

fn sorted_union(left: &[String], right: &[String]) -> Vec<String> {
    left.iter()
        .chain(right)
        .cloned()
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect()
}
