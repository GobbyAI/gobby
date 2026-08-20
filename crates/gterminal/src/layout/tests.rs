use super::*;

fn pane(id: u32) -> PaneId {
    PaneId::from_raw(id)
}

fn sample_layout() -> TileLayout {
    TileLayout::from_saved(
        Node::Split {
            direction: Direction::Horizontal,
            ratio: 0.3,
            first: Box::new(Node::Pane(pane(1))),
            second: Box::new(Node::Split {
                direction: Direction::Vertical,
                ratio: 0.6,
                first: Box::new(Node::Pane(pane(2))),
                second: Box::new(Node::Split {
                    direction: Direction::Horizontal,
                    ratio: 0.4,
                    first: Box::new(Node::Pane(pane(3))),
                    second: Box::new(Node::Pane(pane(4))),
                }),
            }),
        },
        pane(2),
    )
}

fn pane_rects(layout: &TileLayout) -> Vec<(PaneId, Rect)> {
    layout
        .panes(Rect::new(0, 0, 100, 40))
        .into_iter()
        .map(|info| (info.id, info.rect))
        .collect()
}

fn pane_rect(layout: &TileLayout, pane_id: PaneId) -> Rect {
    pane_rects(layout)
        .into_iter()
        .find_map(|(id, rect)| (id == pane_id).then_some(rect))
        .expect("pane should exist")
}

fn split_snapshot(layout: &TileLayout) -> Vec<(Direction, f32)> {
    fn collect(node: &Node, out: &mut Vec<(Direction, f32)>) {
        match node {
            Node::Pane(_) => {}
            Node::Split {
                direction,
                ratio,
                first,
                second,
            } => {
                out.push((*direction, *ratio));
                collect(first, out);
                collect(second, out);
            }
        }
    }

    let mut out = Vec::new();
    collect(layout.root(), &mut out);
    out
}

#[test]
fn swap_panes_exchanges_leaf_ids_without_changing_cells() {
    let mut layout = sample_layout();
    let before_rects = pane_rects(&layout);
    let before_splits = split_snapshot(&layout);

    assert!(layout.swap_panes(pane(2), pane(4)));

    assert_eq!(layout.pane_count(), 4);
    assert_eq!(split_snapshot(&layout), before_splits);
    assert_eq!(layout.focused(), pane(2));

    let after_rects = pane_rects(&layout);
    assert_eq!(after_rects[0], before_rects[0]);
    assert_eq!(after_rects[1], (pane(4), before_rects[1].1));
    assert_eq!(after_rects[2], before_rects[2]);
    assert_eq!(after_rects[3], (pane(2), before_rects[3].1));
}

#[test]
fn swap_panes_is_noop_for_same_or_missing_pane() {
    let mut layout = sample_layout();
    let before_rects = pane_rects(&layout);
    let before_splits = split_snapshot(&layout);
    let before_focus = layout.focused();

    assert!(!layout.swap_panes(pane(2), pane(2)));
    assert!(!layout.swap_panes(pane(2), pane(99)));
    assert!(!layout.swap_panes(pane(99), pane(2)));

    assert_eq!(pane_rects(&layout), before_rects);
    assert_eq!(split_snapshot(&layout), before_splits);
    assert_eq!(layout.focused(), before_focus);
}

#[test]
fn insert_existing_pane_near_target_preserves_existing_ids_and_focuses_moved_pane() {
    let (mut layout, root) = TileLayout::new();
    let moved = pane(99);

    assert!(layout.insert_pane_near(root, moved, Direction::Horizontal, 0.25));

    assert_eq!(layout.pane_count(), 2);
    assert_eq!(layout.pane_ids(), vec![root, moved]);
    assert_eq!(layout.focused(), moved);
    let splits = split_snapshot(&layout);
    assert_eq!(splits, vec![(Direction::Horizontal, 0.25)]);
    assert_eq!(pane_rect(&layout, root), Rect::new(0, 0, 25, 40));
    assert_eq!(pane_rect(&layout, moved), Rect::new(25, 0, 75, 40));
}

#[test]
fn split_focused_with_ratio_sets_new_split_ratio() {
    let (mut layout, root) = TileLayout::new();
    layout.focus_pane(root);

    layout.split_focused_with_ratio(Direction::Horizontal, 0.333);

    let splits = split_snapshot(&layout);
    assert_eq!(splits.len(), 1);
    assert_eq!(splits[0].0, Direction::Horizontal);
    assert!((splits[0].1 - 0.333).abs() < f32::EPSILON);
}

#[test]
fn resize_pane_preserves_focus_and_reports_change() {
    let mut layout = sample_layout();
    let original_focus = layout.focused();

    assert!(layout.resize_pane(pane(1), NavDirection::Right, 0.05, Rect::new(0, 0, 100, 40),));

    assert_eq!(layout.focused(), original_focus);
    let split = split_snapshot(&layout)[0];
    assert_eq!(split.0, Direction::Horizontal);
    assert!((split.1 - 0.35).abs() < f32::EPSILON);
}

#[test]
fn resize_second_child_toward_split_decreases_ratio() {
    let (mut layout, root) = TileLayout::new();
    let right = layout.split_focused(Direction::Horizontal);
    layout.focus_pane(root);

    assert!(layout.resize_pane(right, NavDirection::Left, 0.05, Rect::new(0, 0, 100, 40),));

    let split = split_snapshot(&layout)[0];
    assert_eq!(split.0, Direction::Horizontal);
    assert!((split.1 - 0.45).abs() < f32::EPSILON);
    assert_eq!(layout.focused(), root);
}

#[test]
fn resize_outer_edges_shrink_focused_pane() {
    let (mut horizontal, left) = TileLayout::new();
    horizontal.split_focused(Direction::Horizontal);

    assert!(horizontal.resize_pane(left, NavDirection::Left, 0.05, Rect::new(0, 0, 100, 40),));
    let split = split_snapshot(&horizontal)[0];
    assert_eq!(split.0, Direction::Horizontal);
    assert!((split.1 - 0.45).abs() < f32::EPSILON);

    let (mut horizontal, _left) = TileLayout::new();
    let right = horizontal.split_focused(Direction::Horizontal);

    assert!(horizontal.resize_pane(right, NavDirection::Right, 0.05, Rect::new(0, 0, 100, 40),));
    let split = split_snapshot(&horizontal)[0];
    assert_eq!(split.0, Direction::Horizontal);
    assert!((split.1 - 0.55).abs() < f32::EPSILON);

    let (mut vertical, top) = TileLayout::new();
    vertical.split_focused(Direction::Vertical);

    assert!(vertical.resize_pane(top, NavDirection::Up, 0.05, Rect::new(0, 0, 100, 40),));
    let split = split_snapshot(&vertical)[0];
    assert_eq!(split.0, Direction::Vertical);
    assert!((split.1 - 0.45).abs() < f32::EPSILON);

    let (mut vertical, _top) = TileLayout::new();
    let bottom = vertical.split_focused(Direction::Vertical);

    assert!(vertical.resize_pane(bottom, NavDirection::Down, 0.05, Rect::new(0, 0, 100, 40),));
    let split = split_snapshot(&vertical)[0];
    assert_eq!(split.0, Direction::Vertical);
    assert!((split.1 - 0.55).abs() < f32::EPSILON);
}

#[test]
fn resize_outer_edge_falls_back_to_horizontal_ancestor_split() {
    let mut layout = TileLayout::from_saved(
        Node::Split {
            direction: Direction::Horizontal,
            ratio: 0.6,
            first: Box::new(Node::Split {
                direction: Direction::Vertical,
                ratio: 0.5,
                first: Box::new(Node::Pane(pane(1))),
                second: Box::new(Node::Pane(pane(2))),
            }),
            second: Box::new(Node::Pane(pane(3))),
        },
        pane(1),
    );
    let before = pane_rect(&layout, pane(1));

    assert!(layout.resize_pane(pane(1), NavDirection::Left, 0.05, Rect::new(0, 0, 100, 40),));

    let after = pane_rect(&layout, pane(1));
    assert_eq!(after.height, before.height);
    assert!(after.width < before.width);
    let splits = split_snapshot(&layout);
    assert_eq!(splits[0].0, Direction::Horizontal);
    assert!((splits[0].1 - 0.55).abs() < f32::EPSILON);
    assert_eq!(splits[1], (Direction::Vertical, 0.5));
}

#[test]
fn resize_outer_edge_falls_back_to_vertical_ancestor_split() {
    let mut layout = TileLayout::from_saved(
        Node::Split {
            direction: Direction::Vertical,
            ratio: 0.6,
            first: Box::new(Node::Split {
                direction: Direction::Horizontal,
                ratio: 0.5,
                first: Box::new(Node::Pane(pane(1))),
                second: Box::new(Node::Pane(pane(2))),
            }),
            second: Box::new(Node::Pane(pane(3))),
        },
        pane(1),
    );
    let before = pane_rect(&layout, pane(1));

    assert!(layout.resize_pane(pane(1), NavDirection::Up, 0.05, Rect::new(0, 0, 100, 40),));

    let after = pane_rect(&layout, pane(1));
    assert_eq!(after.width, before.width);
    assert!(after.height < before.height);
    let splits = split_snapshot(&layout);
    assert_eq!(splits[0].0, Direction::Vertical);
    assert!((splits[0].1 - 0.55).abs() < f32::EPSILON);
    assert_eq!(splits[1], (Direction::Horizontal, 0.5));
}

#[test]
fn resize_uses_split_in_same_branch_when_borders_share_coordinate() {
    let mut layout = TileLayout::from_saved(
        Node::Split {
            direction: Direction::Vertical,
            ratio: 0.5,
            first: Box::new(Node::Split {
                direction: Direction::Horizontal,
                ratio: 0.5,
                first: Box::new(Node::Pane(pane(1))),
                second: Box::new(Node::Pane(pane(2))),
            }),
            second: Box::new(Node::Split {
                direction: Direction::Horizontal,
                ratio: 0.5,
                first: Box::new(Node::Pane(pane(3))),
                second: Box::new(Node::Pane(pane(4))),
            }),
        },
        pane(3),
    );

    assert!(layout.resize_pane(pane(3), NavDirection::Right, 0.05, Rect::new(0, 0, 100, 40),));

    let splits = split_snapshot(&layout);
    assert_eq!(splits[0], (Direction::Vertical, 0.5));
    assert_eq!(splits[1], (Direction::Horizontal, 0.5));
    assert_eq!(splits[2].0, Direction::Horizontal);
    assert!((splits[2].1 - 0.55).abs() < f32::EPSILON);
}

#[test]
fn find_in_direction_tiebreaks_by_larger_overlap_before_layout_order() {
    let focused = PaneInfo {
        id: pane(1),
        rect: Rect::new(10, 10, 10, 10),
        inner_rect: Rect::new(10, 10, 10, 10),
        scrollbar_rect: None,
        borders: Borders::NONE,
        is_focused: true,
    };
    let small_overlap_first = PaneInfo {
        id: pane(2),
        rect: Rect::new(0, 10, 10, 2),
        inner_rect: Rect::new(0, 10, 10, 2),
        scrollbar_rect: None,
        borders: Borders::NONE,
        is_focused: false,
    };
    let larger_overlap_second = PaneInfo {
        id: pane(3),
        rect: Rect::new(0, 10, 10, 8),
        inner_rect: Rect::new(0, 10, 10, 8),
        scrollbar_rect: None,
        borders: Borders::NONE,
        is_focused: false,
    };
    let panes = vec![focused.clone(), small_overlap_first, larger_overlap_second];

    assert_eq!(
        find_in_direction(&focused, NavDirection::Left, &panes),
        Some(pane(3))
    );
}
