#[derive(Clone, Copy)]
struct Workflow {
    name: &'static str,
    content: &'static str,
}

const WORKFLOWS: [Workflow; 5] = [
    Workflow {
        name: "ci.yml",
        content: include_str!("../../../.github/workflows/ci.yml"),
    },
    Workflow {
        name: "release-gcode.yml",
        content: include_str!("../../../.github/workflows/release-gcode.yml"),
    },
    Workflow {
        name: "release-gcore.yml",
        content: include_str!("../../../.github/workflows/release-gcore.yml"),
    },
    Workflow {
        name: "release-ghook.yml",
        content: include_str!("../../../.github/workflows/release-ghook.yml"),
    },
    Workflow {
        name: "release-gwiki.yml",
        content: include_str!("../../../.github/workflows/release-gwiki.yml"),
    },
];

#[derive(Debug)]
struct Step<'a> {
    uses: &'a str,
    line_number: usize,
    lines: Vec<&'a str>,
}

#[test]
fn workflow_actions_are_pinned_to_full_commit_shas() {
    for workflow in WORKFLOWS {
        for step in action_steps(workflow.content) {
            if is_local_or_docker_action(step.uses) {
                continue;
            }

            let is_sha_pinned = step
                .uses
                .rsplit_once('@')
                .is_some_and(|(_, reference)| is_full_commit_sha(reference));

            assert!(
                is_sha_pinned,
                "{}:{}: external action ref must be a full 40-character commit SHA: {}",
                workflow.name, step.line_number, step.uses
            );
        }
    }
}

#[test]
fn workflow_action_steps_preserve_security_inputs() {
    for workflow in WORKFLOWS {
        for step in action_steps(workflow.content) {
            if step.uses.starts_with("actions/checkout@") {
                assert!(
                    step_has_line(&step, "persist-credentials: false"),
                    "{}:{}: actions/checkout step must set persist-credentials: false: {}",
                    workflow.name,
                    step.line_number,
                    step.uses
                );
            }

            if step.uses.starts_with("dtolnay/rust-toolchain@") {
                assert!(
                    step_has_line(&step, "toolchain: stable"),
                    "{}:{}: dtolnay/rust-toolchain step must keep toolchain: stable: {}",
                    workflow.name,
                    step.line_number,
                    step.uses
                );
            }

            if step.uses.starts_with("taiki-e/install-action@") {
                assert!(
                    step_has_input(&step, "tool"),
                    "{}:{}: taiki-e/install-action step must set an explicit tool input: {}",
                    workflow.name,
                    step.line_number,
                    step.uses
                );
            }
        }
    }
}

fn action_steps(workflow: &str) -> Vec<Step<'_>> {
    let mut steps = Vec::new();
    let mut current_start = None;
    let mut current_indent = 0;
    let mut current_lines = Vec::new();

    for (index, line) in workflow.lines().enumerate() {
        let line_number = index + 1;
        let indent = leading_spaces(line);
        let trimmed = line.trim_start();
        let starts_list_item = trimmed.starts_with("- ");

        if current_start.is_some()
            && !trimmed.is_empty()
            && indent <= current_indent
            && !starts_list_item
        {
            finish_step(&mut steps, &mut current_start, &mut current_lines);
        }

        if starts_list_item && current_start.is_none_or(|_| indent <= current_indent) {
            finish_step(&mut steps, &mut current_start, &mut current_lines);
            current_start = Some(line_number);
            current_indent = indent;
        }

        if current_start.is_some() {
            current_lines.push(line);
        }
    }

    finish_step(&mut steps, &mut current_start, &mut current_lines);
    steps
}

fn finish_step<'a>(
    steps: &mut Vec<Step<'a>>,
    current_start: &mut Option<usize>,
    current_lines: &mut Vec<&'a str>,
) {
    if let Some(start_line) = current_start.take() {
        push_action_step(steps, start_line, std::mem::take(current_lines));
    }
}

fn push_action_step<'a>(steps: &mut Vec<Step<'a>>, start_line: usize, lines: Vec<&'a str>) {
    let mut uses_line = None;

    for (offset, line) in lines.iter().enumerate() {
        if let Some(uses) = uses_value(line) {
            uses_line = Some((start_line + offset, uses));
            break;
        }
    }

    if let Some((line_number, uses)) = uses_line {
        steps.push(Step {
            uses,
            line_number,
            lines,
        });
    }
}

fn uses_value(line: &str) -> Option<&str> {
    let trimmed = line.trim_start();
    let step_body = trimmed.strip_prefix("- ").unwrap_or(trimmed).trim_start();
    let uses = step_body.strip_prefix("uses:")?.trim();

    Some(uses.trim_matches('"').trim_matches('\''))
}

fn is_local_or_docker_action(uses: &str) -> bool {
    uses.starts_with("./") || uses.starts_with("../") || uses.starts_with("docker://")
}

fn is_full_commit_sha(reference: &str) -> bool {
    reference.len() == 40 && reference.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn step_has_line(step: &Step<'_>, expected: &str) -> bool {
    step.lines.iter().any(|line| line.trim() == expected)
}

fn step_has_input(step: &Step<'_>, input: &str) -> bool {
    let input_prefix = format!("{input}:");

    step.lines.iter().any(|line| {
        line.trim_start()
            .strip_prefix(&input_prefix)
            .is_some_and(|value| !value.trim().is_empty())
    })
}

fn leading_spaces(line: &str) -> usize {
    line.bytes().take_while(|byte| *byte == b' ').count()
}
