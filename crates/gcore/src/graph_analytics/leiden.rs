//! Std-only, deterministic, weighted Leiden community detection.
//!
//! This kernel operates on a decoupled integer-index weighted graph so it can
//! be unit-tested in isolation from the public `AnalyticsGraph`/`Community`
//! types; the façade in `graph_analytics.rs` adapts node ids and memberships.
//!
//! Algorithm: the three-phase Leiden method (Traag, Waltman & van Eck, 2019) —
//! local moving, refinement (which guarantees internally-connected
//! communities), and aggregation — iterated until the graph no longer
//! coarsens. No RNG is used: every choice is a deterministic, ascending,
//! strict-improvement greedy step, so the output is fully reproducible.

use std::collections::{BTreeMap, VecDeque};

/// Default resolution parameter γ (standard modularity).
pub(super) const DEFAULT_GAMMA: f64 = 1.0;

/// Strict-improvement threshold; a move requires a gain greater than this.
const EPS: f64 = 1e-12;

/// Hard cap on aggregation depth (a runaway-recursion backstop).
const MAX_LEVELS: usize = 64;

/// Weighted undirected graph over dense integer node indices `0..n`.
///
/// Invariant established by [`LeidenGraph::new`]: `Σ strength == 2·total_weight`.
/// A self-loop `(u, u, w)` contributes `2w` to `strength[u]` and `w` to
/// `total_weight`, so the invariant survives aggregation — self-loops only
/// arise when an aggregated community's internal edges collapse onto one
/// super-node.
#[derive(Clone)]
pub(super) struct LeidenGraph {
    n: usize,
    /// `adjacency[u]` = ascending `(neighbor, weight)`; a self-loop appears once.
    adjacency: Vec<Vec<(usize, f64)>>,
    /// `strength[u]` = weighted degree (a self-loop counts twice).
    strength: Vec<f64>,
    /// `m` = Σ edge weights (a self-loop counts once).
    total_weight: f64,
}

impl LeidenGraph {
    /// Build from an edge list, folding duplicate `(u, v)` pairs by summation
    /// and sorting each adjacency list by neighbor index for determinism.
    pub(super) fn new(n: usize, edges: &[(usize, usize, f64)]) -> Self {
        let mut acc: Vec<BTreeMap<usize, f64>> = vec![BTreeMap::new(); n];
        let mut strength = vec![0.0_f64; n];
        let mut total_weight = 0.0_f64;
        for &(u, v, w) in edges {
            if u == v {
                *acc[u].entry(u).or_insert(0.0) += w;
                strength[u] += 2.0 * w;
                total_weight += w;
            } else {
                *acc[u].entry(v).or_insert(0.0) += w;
                *acc[v].entry(u).or_insert(0.0) += w;
                strength[u] += w;
                strength[v] += w;
                total_weight += w;
            }
        }
        let adjacency = acc
            .into_iter()
            .map(|m| m.into_iter().collect::<Vec<_>>())
            .collect();
        Self {
            n,
            adjacency,
            strength,
            total_weight,
        }
    }
}

/// A flat community assignment plus per-community total strength (`Σ_tot`).
struct Partition {
    community_of: Vec<usize>,
    sigma_tot: Vec<f64>,
}

impl Partition {
    fn singletons(g: &LeidenGraph) -> Self {
        Self {
            community_of: (0..g.n).collect(),
            sigma_tot: g.strength.clone(),
        }
    }
}

/// One sweep of deterministic local moving. Returns whether any node moved.
///
/// Each node is removed from its community *before* its best target is scored,
/// so candidate `Σ_tot` values are never stale with respect to the moving node.
fn local_moving(g: &LeidenGraph, partition: &mut Partition, gamma: f64) -> bool {
    let two_m = 2.0 * g.total_weight;
    if two_m <= 0.0 {
        return false;
    }
    // Isolation — splitting a node into its own fresh community — needs ids up
    // to `n - 1`, but aggregation-level partitions pack communities densely into
    // `0..k` with `k < n`. Widen `sigma_tot` to `n` and track which ids are
    // unused so a split has a free id to claim.
    partition.sigma_tot.resize(g.n, 0.0);
    let mut comm_size = vec![0usize; g.n];
    for &c in &partition.community_of {
        comm_size[c] += 1;
    }
    let mut free_ids: Vec<usize> = (0..g.n).filter(|&c| comm_size[c] == 0).collect();

    let mut queue: VecDeque<usize> = (0..g.n).collect();
    let mut in_queue = vec![true; g.n];
    let mut moved_any = false;

    while let Some(u) = queue.pop_front() {
        in_queue[u] = false;
        let k_u = g.strength[u];
        let current = partition.community_of[u];

        // Weight of u's edges into each neighboring community (self-loop excluded).
        let mut to_comm: BTreeMap<usize, f64> = BTreeMap::new();
        for &(v, w) in &g.adjacency[u] {
            if v == u {
                continue;
            }
            *to_comm.entry(partition.community_of[v]).or_insert(0.0) += w;
        }

        // Remove u from its community so Σ_tot reflects u's absence.
        partition.sigma_tot[current] -= k_u;
        comm_size[current] -= 1;

        // Baseline: rejoin the current community.
        let k_u_current = to_comm.get(&current).copied().unwrap_or(0.0);
        let mut best_comm = current;
        let mut best_gain = k_u_current - gamma * k_u * partition.sigma_tot[current] / two_m;

        // Candidates are visited in ascending community id, so a tie keeps the
        // smaller id (the strict-improvement test never replaces on a tie).
        for (&comm, &k_u_comm) in &to_comm {
            if comm == current {
                continue;
            }
            let gain = k_u_comm - gamma * k_u * partition.sigma_tot[comm] / two_m;
            if gain > best_gain + EPS {
                best_gain = gain;
                best_comm = comm;
            }
        }

        // Isolation: move u alone into a fresh empty community (gain 0). It is a
        // distinct option only when `current` keeps other members — if removing u
        // already emptied `current`, rejoining it is itself isolation. Tested
        // last and behind strict improvement, so an equally good stay-or-join
        // (gain >= 0) wins over needlessly fragmenting; a size->=2 `current`
        // leaves at most n-1 communities in use, so a free id is guaranteed.
        let target = if comm_size[current] > 0 && 0.0 > best_gain + EPS {
            free_ids
                .pop()
                .expect("a size->=2 community leaves a free id")
        } else {
            best_comm
        };

        // Re-insert u into the chosen community.
        partition.sigma_tot[target] += k_u;
        comm_size[target] += 1;
        partition.community_of[u] = target;

        if target != current {
            // Moving u out may have emptied `current`; recycle its id.
            if comm_size[current] == 0 {
                free_ids.push(current);
            }
            moved_any = true;
            for &(v, _) in &g.adjacency[u] {
                if v != u && !in_queue[v] {
                    in_queue[v] = true;
                    queue.push_back(v);
                }
            }
        }
    }
    moved_any
}

/// Refine `partition` into internally-connected sub-communities.
///
/// Refined communities start as singletons; a node still in a singleton merges
/// into a refined community `R` within the same parent community when `R` is
/// γ-well-connected to the rest of the parent and the merge improves
/// modularity. Because only singletons merge and a merge requires a real
/// connecting edge (`k_{u,R} > 0`), every refined community is internally
/// connected by induction — which is exactly the disconnected-blob weakness the
/// old bridge heuristic worked around.
fn refine_partition(g: &LeidenGraph, partition: &Partition, gamma: f64) -> Partition {
    let n = g.n;
    let two_m = 2.0 * g.total_weight;

    // within_parent[u] = Σ weight of u's edges to other nodes in u's parent.
    let mut within_parent = vec![0.0_f64; n];
    for (u, slot) in within_parent.iter_mut().enumerate() {
        let pu = partition.community_of[u];
        let mut sum = 0.0;
        for &(v, w) in &g.adjacency[u] {
            if v != u && partition.community_of[v] == pu {
                sum += w;
            }
        }
        *slot = sum;
    }

    let mut refined_of: Vec<usize> = (0..n).collect();
    let mut k_refined = g.strength.clone(); // K_R = Σ strength of refined community R
    let mut conn = within_parent.clone(); // connecting weight from R to (C \ R)
    let mut size = vec![1usize; n];

    if two_m <= 0.0 {
        return Partition {
            community_of: refined_of,
            sigma_tot: k_refined,
        };
    }

    for u in 0..n {
        // Only a node still in its own singleton refined community may merge.
        if refined_of[u] != u || size[u] != 1 {
            continue;
        }
        let pu = partition.community_of[u];
        let k_c = partition.sigma_tot[pu]; // K_C = Σ strength of the parent community
        let k_u = g.strength[u];

        // u's edge weight into each refined community within the same parent.
        let mut to_ref: BTreeMap<usize, f64> = BTreeMap::new();
        for &(v, w) in &g.adjacency[u] {
            if v == u || partition.community_of[v] != pu {
                continue;
            }
            *to_ref.entry(refined_of[v]).or_insert(0.0) += w;
        }

        let mut best_comm = u; // stay a singleton
        let mut best_gain = 0.0_f64;
        for (&r, &k_u_r) in &to_ref {
            if r == u {
                continue;
            }
            // Eligibility: R must be γ-well-connected to the rest of its parent,
            // using the global 2m denominator and global strengths.
            let threshold = gamma * k_refined[r] * (k_c - k_refined[r]) / two_m;
            if conn[r] + EPS < threshold {
                continue;
            }
            // Modularity gain of moving the singleton u into R (global m).
            let gain = k_u_r - gamma * k_u * k_refined[r] / two_m;
            if gain > best_gain + EPS {
                best_gain = gain;
                best_comm = r;
            }
        }

        if best_comm != u {
            let k_u_r = to_ref.get(&best_comm).copied().unwrap_or(0.0);
            // R' = R ∪ {u}: edges R→u become internal (−k_{u,R}); u's other
            // within-parent edges become R'→(C\R') (+within_parent[u] − k_{u,R}).
            conn[best_comm] += within_parent[u] - 2.0 * k_u_r;
            k_refined[best_comm] += k_u;
            size[best_comm] += 1;
            refined_of[u] = best_comm;
        }
    }

    Partition {
        community_of: refined_of,
        sigma_tot: k_refined,
    }
}

/// Build the super-graph whose nodes are the refined communities, plus the
/// initial partition for the next level (each super-node inherits the
/// local-moving community of its smallest-index member) and the node→super map.
fn aggregate_graph(
    g: &LeidenGraph,
    refined: &Partition,
    partition: &Partition,
) -> (LeidenGraph, Partition, Vec<usize>) {
    let n = g.n;
    // Dense-relabel refined community ids by first-seen ascending node index.
    let mut dense: Vec<Option<usize>> = vec![None; n];
    let mut reps: Vec<usize> = Vec::new(); // reps[super_id] = representative node
    let mut super_of = vec![0usize; n];
    for (u, slot) in super_of.iter_mut().enumerate() {
        let r = refined.community_of[u];
        let sid = match dense[r] {
            Some(s) => s,
            None => {
                let s = reps.len();
                dense[r] = Some(s);
                reps.push(u);
                s
            }
        };
        *slot = sid;
    }
    let super_n = reps.len();

    // Accumulate super-edges, iterating each undirected edge once (v >= u).
    // Intra-community weight collapses onto a super self-loop (su == sv).
    let mut acc: BTreeMap<(usize, usize), f64> = BTreeMap::new();
    for u in 0..n {
        let su = super_of[u];
        for &(v, w) in &g.adjacency[u] {
            if v < u {
                continue;
            }
            let sv = super_of[v];
            let key = if su <= sv { (su, sv) } else { (sv, su) };
            *acc.entry(key).or_insert(0.0) += w;
        }
    }
    let edges: Vec<(usize, usize, f64)> = acc.into_iter().map(|((a, b), w)| (a, b, w)).collect();
    let super_graph = LeidenGraph::new(super_n, &edges);

    // Initial partition: each super-node inherits its representative's community.
    let community_of: Vec<usize> = reps
        .iter()
        .map(|&rep| partition.community_of[rep])
        .collect();
    let mut super_partition = Partition {
        community_of,
        sigma_tot: Vec::new(),
    };
    renumber_dense(&mut super_partition, &super_graph);

    (super_graph, super_partition, super_of)
}

/// Relabel community ids to `0..k` (first-seen ascending) and rebuild `Σ_tot`.
fn renumber_dense(partition: &mut Partition, g: &LeidenGraph) {
    let mut remap: BTreeMap<usize, usize> = BTreeMap::new();
    let mut next = 0usize;
    for c in partition.community_of.iter_mut() {
        let new = match remap.get(c) {
            Some(&x) => x,
            None => {
                let x = next;
                remap.insert(*c, x);
                next += 1;
                x
            }
        };
        *c = new;
    }
    let mut sigma = vec![0.0_f64; next];
    for u in 0..g.n {
        sigma[partition.community_of[u]] += g.strength[u];
    }
    partition.sigma_tot = sigma;
}

/// Detect communities via multi-level weighted Leiden.
///
/// Returns `memberships`, where `memberships[u]` is the dense community id of
/// node `u` (relabeled first-seen ascending). Nodes with no usable edges fall
/// out as their own singleton communities.
pub(super) fn detect_communities(g: &LeidenGraph, gamma: f64) -> Vec<usize> {
    if g.n == 0 {
        return Vec::new();
    }
    if 2.0 * g.total_weight <= 0.0 {
        // No usable edges: every node is its own community.
        return (0..g.n).collect();
    }

    let mut current = g.clone();
    let mut partition = Partition::singletons(&current);
    // level_maps[level][node] = super-node index at the next level.
    let mut level_maps: Vec<Vec<usize>> = Vec::new();

    for _ in 0..MAX_LEVELS {
        local_moving(&current, &mut partition, gamma);
        let refined = refine_partition(&current, &partition, gamma);
        let (super_graph, super_partition, super_of) =
            aggregate_graph(&current, &refined, &partition);
        if super_graph.n == current.n {
            // Refinement merged nothing: no further coarsening is possible, and
            // `partition` already holds the best partition at this granularity.
            break;
        }
        level_maps.push(super_of);
        current = super_graph;
        partition = super_partition;
    }

    // Project the top-level partition back down through the aggregation maps.
    let mut membership = partition.community_of.clone();
    for map in level_maps.iter().rev() {
        let mut lower = vec![0usize; map.len()];
        for (low, &high) in map.iter().enumerate() {
            lower[low] = membership[high];
        }
        membership = lower;
    }

    dense_relabel(&mut membership);
    membership
}

/// Relabel memberships to `0..k` by first-seen ascending node index.
fn dense_relabel(membership: &mut [usize]) {
    let mut remap: BTreeMap<usize, usize> = BTreeMap::new();
    let mut next = 0usize;
    for c in membership.iter_mut() {
        let new = match remap.get(c) {
            Some(&x) => x,
            None => {
                let x = next;
                remap.insert(*c, x);
                next += 1;
                x
            }
        };
        *c = new;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `Σ strength == 2·total_weight` — the invariant that keeps modularity
    /// well-defined at every aggregation level.
    fn assert_strength_invariant(g: &LeidenGraph) {
        let sum: f64 = g.strength.iter().sum();
        assert!(
            (sum - 2.0 * g.total_weight).abs() < 1e-9,
            "Σstrength={sum} != 2m={}",
            2.0 * g.total_weight
        );
    }

    /// BFS each community over its intra-community edges; assert connectivity.
    fn assert_communities_connected(n: usize, edges: &[(usize, usize, f64)], membership: &[usize]) {
        let mut adj = vec![Vec::new(); n];
        for &(u, v, _) in edges {
            if u != v && membership[u] == membership[v] {
                adj[u].push(v);
                adj[v].push(u);
            }
        }
        let count = membership.iter().copied().max().map_or(0, |m| m + 1);
        for comm in 0..count {
            let members: Vec<usize> = (0..n).filter(|&u| membership[u] == comm).collect();
            if members.len() <= 1 {
                continue;
            }
            let start = members[0];
            let mut seen = vec![false; n];
            let mut stack = vec![start];
            seen[start] = true;
            let mut reached = 0;
            while let Some(node) = stack.pop() {
                reached += 1;
                for &next in &adj[node] {
                    if !seen[next] {
                        seen[next] = true;
                        stack.push(next);
                    }
                }
            }
            assert_eq!(
                reached,
                members.len(),
                "community {comm} is not internally connected"
            );
        }
    }

    fn detect(n: usize, edges: &[(usize, usize, f64)]) -> Vec<usize> {
        let g = LeidenGraph::new(n, edges);
        detect_communities(&g, DEFAULT_GAMMA)
    }

    fn community_count(membership: &[usize]) -> usize {
        membership.iter().copied().max().map_or(0, |m| m + 1)
    }

    fn triangle(base: usize) -> Vec<(usize, usize, f64)> {
        vec![
            (base, base + 1, 1.0),
            (base + 1, base + 2, 1.0),
            (base + 2, base, 1.0),
        ]
    }

    fn clique(base: usize, size: usize, weight: f64) -> Vec<(usize, usize, f64)> {
        let mut edges = Vec::new();
        for i in 0..size {
            for j in (i + 1)..size {
                edges.push((base + i, base + j, weight));
            }
        }
        edges
    }

    /// Newman modularity of a flat partition from the symmetric adjacency.
    /// The test graphs here are level-0 (no self-loops), so
    /// `Q = internal_double/2m − γ·Σ_c (D_c/2m)²`, where `internal_double`
    /// counts each internal edge twice (once per direction) and `D_c` is the
    /// summed strength of community `c`.
    fn modularity(g: &LeidenGraph, membership: &[usize], gamma: f64) -> f64 {
        let two_m = 2.0 * g.total_weight;
        if two_m <= 0.0 {
            return 0.0;
        }
        let mut internal_double = 0.0;
        for u in 0..g.n {
            for &(v, w) in &g.adjacency[u] {
                if v != u && membership[v] == membership[u] {
                    internal_double += w;
                }
            }
        }
        let k = membership.iter().copied().max().map_or(0, |m| m + 1);
        let mut degree = vec![0.0_f64; k];
        for u in 0..g.n {
            degree[membership[u]] += g.strength[u];
        }
        let null: f64 = degree.iter().map(|d| (d / two_m) * (d / two_m)).sum();
        internal_double / two_m - gamma * null
    }

    /// Zachary's karate club — the 78 canonical NetworkX edges over 34 nodes,
    /// 0-indexed, each weight 1.0. The 4-community modularity optimum is
    /// `Q ≈ 0.4198`; the 2-faction split scores `≈ 0.371`.
    fn karate_club() -> Vec<(usize, usize, f64)> {
        // Grouped by source node (kept hand-laid for auditability against the
        // canonical adjacency: node 0 has degree 16, node 33 degree 17).
        #[rustfmt::skip]
        let pairs: &[(usize, usize)] = &[
            (0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (0, 6), (0, 7), (0, 8),
            (0, 10), (0, 11), (0, 12), (0, 13), (0, 17), (0, 19), (0, 21), (0, 31),
            (1, 2), (1, 3), (1, 7), (1, 13), (1, 17), (1, 19), (1, 21), (1, 30),
            (2, 3), (2, 7), (2, 8), (2, 9), (2, 13), (2, 27), (2, 28), (2, 32),
            (3, 7), (3, 12), (3, 13),
            (4, 6), (4, 10),
            (5, 6), (5, 10), (5, 16),
            (6, 16),
            (8, 30), (8, 32), (8, 33),
            (9, 33),
            (13, 33),
            (14, 32), (14, 33),
            (15, 32), (15, 33),
            (18, 32), (18, 33),
            (19, 33),
            (20, 32), (20, 33),
            (22, 32), (22, 33),
            (23, 25), (23, 27), (23, 29), (23, 32), (23, 33),
            (24, 25), (24, 27), (24, 31),
            (25, 31),
            (26, 29), (26, 33),
            (27, 33),
            (28, 31), (28, 33),
            (29, 32), (29, 33),
            (30, 32), (30, 33),
            (31, 32), (31, 33),
            (32, 33),
        ];
        pairs.iter().map(|&(u, v)| (u, v, 1.0)).collect()
    }

    /// Brute-force local-optimality check: for every node, evaluate every move
    /// to an existing community and to a fresh singleton id (isolation), and
    /// assert none raises `Q` beyond a `1e-9` tolerance. This is the strongest
    /// single-node correctness invariant — it directly catches a missed
    /// isolation move.
    fn assert_no_improving_single_move(g: &LeidenGraph, membership: &[usize], gamma: f64) {
        let base = modularity(g, membership, gamma);
        let fresh = membership.iter().copied().max().map_or(0, |m| m + 1);
        for u in 0..g.n {
            let original = membership[u];
            for target in 0..=fresh {
                if target == original {
                    continue;
                }
                let mut trial = membership.to_vec();
                trial[u] = target;
                let q = modularity(g, &trial, gamma);
                assert!(
                    q <= base + 1e-9,
                    "node {u}: moving to community {target} raises Q from {base} to {q}"
                );
            }
        }
    }

    #[test]
    fn two_triangles_with_bridge_split() {
        let mut edges = triangle(0);
        edges.extend(triangle(3));
        edges.push((2, 3, 1.0)); // bridge
        let membership = detect(6, &edges);
        assert_eq!(community_count(&membership), 2);
        assert_eq!(membership[0], membership[1]);
        assert_eq!(membership[1], membership[2]);
        assert_eq!(membership[3], membership[4]);
        assert_eq!(membership[4], membership[5]);
        assert_ne!(membership[0], membership[3]);
        assert_communities_connected(6, &edges, &membership);
    }

    #[test]
    fn two_cliques_recover_planted_partition() {
        let mut edges = clique(0, 5, 1.0);
        edges.extend(clique(5, 5, 1.0));
        edges.push((0, 5, 1.0));
        edges.push((4, 9, 1.0));
        let membership = detect(10, &edges);
        assert_eq!(community_count(&membership), 2);
        for node in 1..5 {
            assert_eq!(membership[0], membership[node]);
        }
        for node in 6..10 {
            assert_eq!(membership[5], membership[node]);
        }
        assert_ne!(membership[0], membership[5]);
        assert_communities_connected(10, &edges, &membership);
    }

    #[test]
    fn no_edge_multi_node_graph_is_all_singletons() {
        let membership = detect(3, &[]);
        assert_eq!(membership, vec![0, 1, 2]);
    }

    #[test]
    fn empty_graph_returns_empty() {
        assert_eq!(detect(0, &[]), Vec::<usize>::new());
    }

    #[test]
    fn single_node_returns_one_community() {
        assert_eq!(detect(1, &[]), vec![0]);
    }

    #[test]
    fn triangle_with_isolated_node_yields_two_communities() {
        let edges = triangle(0);
        let membership = detect(4, &edges);
        assert_eq!(community_count(&membership), 2);
        assert_eq!(membership[0], membership[1]);
        assert_eq!(membership[1], membership[2]);
        assert_ne!(membership[0], membership[3]);
    }

    #[test]
    fn four_node_path_is_not_all_singletons() {
        let edges = [(0, 1, 1.0), (1, 2, 1.0), (2, 3, 1.0)];
        let membership = detect(4, &edges);
        assert!(
            community_count(&membership) < 4,
            "path collapsed to {} singletons",
            community_count(&membership)
        );
        assert_communities_connected(4, &edges, &membership);
    }

    #[test]
    fn duplicate_and_reciprocal_edges_fold_by_summation() {
        let g = LeidenGraph::new(2, &[(0, 1, 1.0), (1, 0, 2.0)]);
        assert_eq!(g.adjacency[0], vec![(1, 3.0)]);
        assert_eq!(g.adjacency[1], vec![(0, 3.0)]);
        assert_eq!(g.strength, vec![3.0, 3.0]);
        assert_eq!(g.total_weight, 3.0);
        assert_strength_invariant(&g);
    }

    #[test]
    fn self_loop_contributes_double_strength_single_weight() {
        let g = LeidenGraph::new(2, &[(0, 0, 2.0), (0, 1, 1.0)]);
        // The self-loop appears once in the adjacency list.
        assert_eq!(g.adjacency[0], vec![(0, 2.0), (1, 1.0)]);
        assert_eq!(g.strength[0], 5.0); // 2*2.0 (loop) + 1.0 (edge)
        assert_eq!(g.strength[1], 1.0);
        assert_eq!(g.total_weight, 3.0); // 2.0 (loop) + 1.0 (edge)
        assert_strength_invariant(&g);
    }

    #[test]
    fn aggregation_preserves_strength_invariant_and_total_weight() {
        let mut edges = triangle(0);
        edges.extend(triangle(3));
        edges.push((2, 3, 1.0));
        let g = LeidenGraph::new(6, &edges);
        let original_total = g.total_weight;

        let mut partition = Partition::singletons(&g);
        local_moving(&g, &mut partition, DEFAULT_GAMMA);
        let refined = refine_partition(&g, &partition, DEFAULT_GAMMA);
        let (super_graph, _, _) = aggregate_graph(&g, &refined, &partition);

        assert_strength_invariant(&super_graph);
        assert!((super_graph.total_weight - original_total).abs() < 1e-9);
    }

    #[test]
    fn multi_level_back_projection_maps_original_nodes() {
        // Four triangles chained weakly forces more than one aggregation level
        // and exercises the composed level maps during back-projection.
        let mut edges = Vec::new();
        for cluster in 0..4 {
            edges.extend(triangle(cluster * 3));
        }
        edges.push((2, 3, 0.1));
        edges.push((5, 6, 0.1));
        edges.push((8, 9, 0.1));
        let membership = detect(12, &edges);
        // Each triangle stays intact.
        for cluster in 0..4 {
            let base = cluster * 3;
            assert_eq!(membership[base], membership[base + 1]);
            assert_eq!(membership[base + 1], membership[base + 2]);
        }
        assert_eq!(community_count(&membership), 4);
        assert_communities_connected(12, &edges, &membership);
    }

    #[test]
    fn output_is_deterministic_across_repeats() {
        let mut edges = clique(0, 5, 1.0);
        edges.extend(clique(5, 5, 1.0));
        edges.push((0, 5, 1.0));
        let first = detect(10, &edges);
        for _ in 0..10 {
            assert_eq!(detect(10, &edges), first);
        }
    }

    #[test]
    fn output_is_invariant_to_edge_list_order() {
        let mut edges = clique(0, 5, 1.0);
        edges.extend(clique(5, 5, 1.0));
        edges.push((0, 5, 1.0));
        edges.push((4, 9, 1.0));
        let baseline = detect(10, &edges);

        // Folding + sorting in LeidenGraph::new must erase input order; a
        // deterministic reversal stands in for a shuffle.
        let mut reversed = edges.clone();
        reversed.reverse();
        assert_eq!(detect(10, &reversed), baseline);
    }

    #[test]
    fn weighting_pulls_a_node_toward_the_heavier_community() {
        // Node 0 touches both clusters; the heavier edge should win it.
        let mut edges = clique(1, 3, 1.0);
        edges.extend(clique(4, 3, 1.0));
        edges.push((0, 1, 0.5)); // weak tie to the first cluster
        edges.push((0, 4, 5.0)); // strong tie to the second cluster
        let membership = detect(7, &edges);
        assert_eq!(membership[0], membership[4]);
        assert_communities_connected(7, &edges, &membership);
    }

    #[test]
    fn local_moving_isolates_over_merged_self_loop_cluster() {
        // Two heavy super-nodes only faintly tied together are wedged into one
        // community, as aggregation can leave them. Each self-loop makes pulling
        // a node out into its own singleton a strict modularity win — a move the
        // local-moving pass can realize only if it offers a fresh empty
        // community alongside the current and neighboring ones. Without that
        // candidate the pair stays fused; with it, the cluster splits.
        let g = LeidenGraph::new(2, &[(0, 0, 10.0), (1, 1, 10.0), (0, 1, 0.1)]);
        let mut partition = Partition {
            community_of: vec![0, 0],
            sigma_tot: vec![40.2, 0.0],
        };
        local_moving(&g, &mut partition, DEFAULT_GAMMA);
        assert_ne!(
            partition.community_of[0], partition.community_of[1],
            "an isolation move must split the over-merged self-loop cluster"
        );
    }

    #[test]
    fn zachary_karate_club_finds_modular_structure() {
        let edges = karate_club();
        let g = LeidenGraph::new(34, &edges);
        let membership = detect_communities(&g, DEFAULT_GAMMA);
        let q = modularity(&g, &membership, DEFAULT_GAMMA);
        let count = community_count(&membership);
        // Optimum ≈ 0.4198; the 2-faction split ≈ 0.371; a trivial partition = 0.
        // `q >= 0.40` is the real "it found modular structure" signal.
        assert!(
            (2..=5).contains(&count),
            "unexpected community count {count}"
        );
        assert!(q >= 0.40, "modularity Q={q} far below the ~0.42 optimum");
        assert!(
            q <= 0.42 + 1e-6,
            "modularity Q={q} exceeds theoretical optimum"
        );
        assert_communities_connected(34, &edges, &membership);
        assert_eq!(detect_communities(&g, DEFAULT_GAMMA), membership); // deterministic
    }

    #[test]
    fn local_moving_partition_has_no_improving_single_node_move() {
        // The full multi-level pipeline output is NOT guaranteed single-node
        // optimal: higher aggregation levels move whole groups, which can leave
        // an individual node with an improving move (on karate, node 9 →
        // +0.001 Q toward the 0.41979 optimum). The level-0 `local_moving`
        // pass, by contrast, converges to a strict single-node optimum by
        // construction — including the isolation (fresh-singleton) candidate —
        // so it is the right scope for this invariant: a missed isolation move
        // surfaces here as an improving move the pass failed to take.
        let two_cliques = {
            let mut e = clique(0, 5, 1.0);
            e.extend(clique(5, 5, 1.0));
            e.push((0, 5, 1.0));
            e.push((4, 9, 1.0));
            (10usize, e)
        };
        for (n, edges) in [two_cliques, (34usize, karate_club())] {
            let g = LeidenGraph::new(n, &edges);
            let mut partition = Partition::singletons(&g);
            local_moving(&g, &mut partition, DEFAULT_GAMMA);
            // Guard against a vacuous pass: local moving must form real
            // structure, neither leaving every node a singleton nor collapsing
            // all into one. Count distinct ids (the raw output is not densely
            // numbered) rather than `community_count`'s max-id heuristic.
            let distinct = partition
                .community_of
                .iter()
                .copied()
                .collect::<std::collections::BTreeSet<usize>>()
                .len();
            assert!(
                1 < distinct && distinct < n,
                "local moving produced a degenerate partition: {distinct} communities over {n} nodes"
            );
            assert_no_improving_single_move(&g, &partition.community_of, DEFAULT_GAMMA);
        }
    }
}
