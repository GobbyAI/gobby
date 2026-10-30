//! Unicode case folding shared by Python-compatible daemon contracts.

#[path = "casefold/table.rs"]
mod table;

/// Python's `str.casefold`; lowercase alone misses sharp s, final sigma and Cherokee.
pub(crate) fn casefold(value: &str) -> String {
    let mut folded = String::with_capacity(value.len());
    for c in value.chars() {
        match table::CASEFOLD_TABLE.binary_search_by_key(&c, |&(from, _)| from) {
            Ok(index) => folded.push_str(table::CASEFOLD_TABLE[index].1),
            Err(_) => folded.push(c),
        }
    }
    folded
}

#[cfg(test)]
#[path = "casefold/tests.rs"]
mod tests;
