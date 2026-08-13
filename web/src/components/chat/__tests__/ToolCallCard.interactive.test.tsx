import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ToolCall } from "../../../types/chat";
import { classifyTool } from "../../../types/chat";
import { renderWithProviders, screen } from "../../../test/helpers";
import { MarkdownBody } from "../../shared/MarkdownBody";
import { ToolCallCards } from "../ToolCallCard";

const cwd = process.cwd();

function readSource(rel: string): string {
  return readFileSync(join(cwd, rel), "utf8");
}

function makeCall(
  overrides: Partial<ToolCall> & { id: string; tool_name: string },
): ToolCall {
  return {
    server_name: "builtin",
    status: "completed",
    tool_type: classifyTool(overrides.tool_name),
    ...overrides,
  };
}

describe("ToolCallCard interactions", () => {
  it("expands a single tool card with the keyboard and exposes its result", () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: "read-1",
            tool_name: "Read",
            status: "completed",
            arguments: { file_path: "/tmp/example.txt" },
            result: {
              content: "keyboard-accessible result",
              kind: "text",
              truncated: false,
            },
          }),
        ]}
      />,
    );

    const header = screen.getByRole("button", { name: /Read/ });
    expect(header).toHaveAttribute("tabindex", "0");
    expect(header).toHaveAttribute("aria-expanded", "false");

    header.focus();
    fireEvent.keyDown(header, { key: "Enter" });

    expect(header).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("keyboard-accessible result")).toBeInTheDocument();
  });

  it("toggles a grouped tool-call header with Space and Enter", () => {
    const calls = ["one", "two", "three"].map((content, index) =>
      makeCall({
        id: `read-group-${index}`,
        tool_name: "Read",
        status: "completed",
        result: { content, kind: "text", truncated: false },
      }),
    );
    renderWithProviders(<ToolCallCards toolCalls={calls} />);

    const header = screen.getByRole("button", { name: /Read.*×3/ });
    expect(header).toHaveAttribute("tabindex", "0");
    expect(header).toHaveAttribute("aria-expanded", "true");

    header.focus();
    fireEvent.keyDown(header, { key: " " });
    expect(header).toHaveAttribute("aria-expanded", "false");

    fireEvent.keyDown(header, { key: "Enter" });
    expect(header).toHaveAttribute("aria-expanded", "true");
  });

  it("dispatches approval once and disables every decision button after success", () => {
    const onRespondToApproval = vi.fn(() => true);

    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: "approval-1",
            tool_name: "Bash",
            status: "pending_approval",
          }),
        ]}
        onRespondToApproval={onRespondToApproval}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(onRespondToApproval).toHaveBeenCalledOnce();
    expect(onRespondToApproval).toHaveBeenCalledWith("approval-1", "approve");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Always Approve" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onRespondToApproval).toHaveBeenCalledOnce();
  });

  it("shows a disconnect error and leaves decision buttons available for retry", () => {
    const onRespondToApproval = vi.fn(() => false);

    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: "approval-2",
            tool_name: "Bash",
            status: "pending_approval",
          }),
        ]}
        onRespondToApproval={onRespondToApproval}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));

    expect(onRespondToApproval).toHaveBeenCalledWith("approval-2", "reject");
    expect(
      screen.getByText("Disconnected — reconnecting..."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("submits multi-select and Other answers once, then disables the choices", () => {
    const onRespond = vi.fn(() => true);
    const questions = [
      {
        header: "Tools",
        question: "Which tools?",
        multiSelect: true,
        options: [
          { label: "Read", description: "Inspect files" },
          { label: "Edit", description: "Change files" },
        ],
      },
      {
        header: "Note",
        question: "Anything else?",
        multiSelect: false,
        options: [{ label: "Nothing", description: "No extra note" }],
      },
    ];

    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: "question-1",
            tool_name: "AskUserQuestion",
            status: "calling",
            arguments: { questions },
          }),
        ]}
        onRespond={onRespond}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Read/ }));
    fireEvent.click(screen.getByRole("button", { name: /Edit/ }));
    fireEvent.click(screen.getAllByRole("button", { name: "Other" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "Other" })[1]);
    const customInputs = screen.getAllByPlaceholderText("Type your answer...");
    fireEvent.change(customInputs[0], {
      target: { value: "Search" },
    });
    fireEvent.change(customInputs[1], {
      target: { value: "Use the indexed search first" },
    });
    expect(screen.getByRole("button", { name: /Read/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getAllByRole("button", { name: "Other" })[0]).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    expect(onRespond).toHaveBeenCalledOnce();
    expect(onRespond).toHaveBeenCalledWith("question-1", {
      "Which tools?": "Read, Edit, Search",
      "Anything else?": "Use the indexed search first",
    });
    expect(screen.getByRole("button", { name: /Read/ })).toBeDisabled();
    expect(screen.getAllByRole("button", { name: "Other" })[0]).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "Submit" }),
    ).not.toBeInTheDocument();
  });

  it("does not submit when a selected Other answer is empty", () => {
    const onRespond = vi.fn(() => true);
    const questions = [
      {
        header: "Note",
        question: "Anything else?",
        multiSelect: false,
        options: [{ label: "Nothing", description: "No extra note" }],
      },
    ];

    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: "question-empty-other",
            tool_name: "AskUserQuestion",
            status: "calling",
            arguments: { questions },
          }),
        ]}
        onRespond={onRespond}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Other" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(onRespond).not.toHaveBeenCalled();

    fireEvent.keyDown(screen.getByPlaceholderText("Type your answer..."), {
      key: "Enter",
    });
    expect(onRespond).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Submit" })).toBeInTheDocument();
  });

  it("renders array-valued answers from completed question results", () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: "question-2",
            tool_name: "AskUserQuestion",
            status: "completed",
            arguments: {
              questions: [
                {
                  header: "Tools",
                  question: "Which tools?",
                  multiSelect: true,
                  options: [{ label: "Read" }, { label: "Edit" }],
                },
              ],
            },
            result: {
              kind: "json",
              content: { answers: { "Which tools?": ["Read", "Edit"] } },
              truncated: false,
            },
          }),
        ]}
      />,
    );

    expect(screen.getByText("Answered")).toBeInTheDocument();
    expect(screen.getByText("Read").parentElement).toHaveClass("border-accent");
    expect(screen.getByText("Edit").parentElement).toHaveClass("border-accent");
    expect(screen.queryByText("Render error")).not.toBeInTheDocument();
  });
});

describe("MarkdownBody typography ownership", () => {
  it("stays wrapper-neutral while every message-content host adopts the shared utility", () => {
    const { container } = renderWithProviders(
      <MarkdownBody
        content={"First paragraph\n\nSecond paragraph"}
        id="fragment-contract"
      />,
    );

    expect(Array.from(container.children, (child) => child.tagName)).toEqual([
      "P",
      "P",
    ]);

    const sharedSource = readSource("src/components/shared/MarkdownBody.tsx");
    expect(sharedSource).toContain("export const markdownBodyClassName");
    expect(sharedSource).toContain('"max-w-[70ch]"');
    expect(sharedSource).toContain('"[&_h1]:text-[length:var(--text-3xl)]"');

    const hosts = [
      "src/components/chat/MessageItem.tsx",
      "src/components/activity/FilesTab.tsx",
      "src/components/activity/PlanReviewCard.tsx",
      "src/components/activity/SessionsTabDetail.tsx",
      "src/components/activity/TasksTabDetailPanel.tsx",
      "src/components/activity/skills/SkillContentView.tsx",
      "src/components/activity/taskdetail/TaskDetailEditableCore.tsx",
      "src/components/activity/wiki/WikiPageReader.tsx",
    ];
    for (const host of hosts) {
      expect(readSource(host), host).toContain("markdownBodyClassName");
    }
  });

  it("leaves ToolCallCard and RichContentBlocks direct consumers unchanged", () => {
    const { container } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: "direct-markdown",
            tool_name: "ExitPlanMode",
            arguments: { plan: "# Direct consumer heading" },
          }),
        ]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /ExitPlanMode/ }));
    const heading = screen.getByRole("heading", {
      name: "Direct consumer heading",
    });
    expect(heading.tagName).toBe("H1");
    expect(heading.parentElement).not.toHaveAttribute("class");
    expect(container.querySelector(".message-content")).toBeNull();

    const directConsumers = {
      "src/components/chat/ToolCallCardContent.tsx": [
        "<MarkdownBody id={`tool-plan-${callId}`} content={args.plan} />",
        "<MarkdownBody content={resultStr} id={`tool-result-${call.id}`} />",
      ],
      "src/components/chat/RichContentBlocks.tsx": [
        "return <MarkdownBody id={id} content={block.content} />",
      ],
      "src/components/chat/Markdown.tsx": [
        'export { MarkdownBody as Markdown } from "../shared/MarkdownBody";',
      ],
    };
    for (const [path, pins] of Object.entries(directConsumers)) {
      const source = readSource(path);
      expect(source, path).not.toContain("markdownBodyClassName");
      for (const pin of pins) expect(source, path).toContain(pin);
    }
  });
});
