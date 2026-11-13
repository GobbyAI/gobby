# Review lessons: are they worth keeping?

Researcher gobby#14435, 2026-09-24. Research only: read-only database queries,
transcript and log reads, and code reading. Nothing was changed. The PD disabled
the touched-files rule's installed row at 19:21 UTC on 2026-09-24; I did not touch it.

## Request

From Josh, relayed by the Assistant (#14069): "let's disable that rule. I want to
investigate whether it's actually of value, review lessons." And: "I don't know if
anything new has been added to review lessons since we switched from coderabbit to
ocr." The four questions: what exists and what has been recorded since the switch;
whether there is evidence the lessons changed behaviour or caught repeats; what each
delivery path costs; and keep, trim or retire, with the least mechanism.

## Answer

- **Yes, lessons were added after the switch, but recording has almost stopped.**
  The switch to OCR landed on 09-11 (#22166, "Replace code-reviewer skill with
  code-review skill wrapping ocr delegate mode and gate first commit on it"). From
  09-11 to 09-19, 75 lessons were recorded. Since 09-20, only 3. That drop has a
  cause: on 09-20 the queued close reviewer (#22636, "Implement OCR-assisted queued
  task close reviewer") took over most review work. It is required to read the
  lessons guide, but it is not allowed to call either lesson tool.
- **No evidence that pushed lessons change behaviour.** In about 760 injections of
  the touched-files rule over 30 days, an agent cited the injected lesson once. The
  one same-pattern repeat on an injected path is a repeat the injection did not
  prevent. Prevention itself can't be measured, because nothing records what an
  injection led to.
- **Retire the pushes. Keep the corpus and the pull tools.** Retire the
  touched-files rule and all four class rules from #18730 ("Push injection rules
  for plan and QA agents"). Stop telling the close reviewer to use tools it can't
  call. Keep the 251 lessons, `record_review_lesson`, and recall in the pre-commit
  code-review flow. That flow has also been nearly idle since 09-21, so it comes with
  a stated condition for retiring it later (Q4).

## Q1. What exists, and what has been recorded since the switch

There are 251 review-lesson memories, created 2026-06-02 to 09-24, and none is
archived. 237 are marked confirmed and 7 no-fix-policy. They hold 229 distinct
pattern ids.

| Period | New lessons | Main sources |
| --- | --- | --- |
| 06-02 to 09-10 (before OCR) | 173 | 115 from CodeRabbit. The last CodeRabbit lesson was 08-19; none at all 08-20 to 09-06 |
| 09-11 to 09-19 (OCR, pre-commit) | 75 | 37 OCR or pre-commit reviews, 18 docs reference audits, 20 other |
| 09-20 to 09-24 (close reviewer live) | 3 | 09-21 ocr-precommit, 09-21 gobby-landing-review, 09-24 pre-commit-self-review |

By source kind (245 tagged): agent_review 119, review_comment 91, qa_rejection 10,
test_failure 9, static_analysis 8, plan_review 8.

**Why recording stopped.** The close reviewer ran 287 times from 09-21 to 09-24. Its
agent definition requires `review-lessons.md` (`task-close-reviewer.yaml:163`) and
the code-review skill, and both tell it to call `recall_review_context` and
`record_review_lesson` (`code-review/SKILL.md:154-164`). Neither tool is in its
review-step allowlist (`task-close-reviewer.yaml:238-246`). Real tool calls, not
mentions:

| Calls | 09-11 to 09-20 | 09-21 to 09-24 |
| --- | --- | --- |
| Claude `recall_review_context` | 45 | 0 |
| Claude `record_review_lesson` | 20 | 1 |
| Codex `recall_review_context`, returned results | 134 | 54 (49 of them on 09-21) |
| Codex `recall_review_context`, denied by a step allowlist | 4 | 46 |
| Codex `record_review_lesson`, recorded | 56 | 2 (both on 09-21) |

Codex agents call tools through a code-mode `exec`, so the Codex counts come from
the `call_tool(... tool_name: ...)` text inside each exec and its matching output.
All 46 denials since 09-21 come from the task-close reviewer and read: "MCP tool
'gobby-review-learning:recall_review_context' is not allowed in the 'review' step."
One Codex reviewer also filed feedback saying so. The gap is older than the new
reviewer: the task-close validator was denied the same call once on 09-11. #22636
made it matter by routing most reviews through a step without the tools. The gap is
tracked as #22822 ("Allow required review-learning recall in task-close reviews").

## Q2. Evidence that lessons changed behaviour or caught repeats

**The touched-files rule** (#15560, "Add file-scoped review lesson guardrail"). Claude
transcripts from 08-24 to 09-24 (the 30-day retention floor) hold 760 injections.
Together they carry 943 lesson slots and 145 distinct patterns on about 200 paths.
They span 183 sessions, or 271 transcripts when subagents are counted separately.

- **Uptake.** The injected pattern id appeared in the next 3 assistant messages
  once. The words "lesson" or "review-guidance" appeared after about 6% of
  injections. That is an upper bound, and most of those hits are sessions that were
  examining the lesson system itself.
- **Repeats.** 13 slots were followed by a later lesson on the same path. Only 1 was
  the same pattern: `tui-selection-confirm-visible-row` on `prompt_detector.py`, on
  09-12, the same day as the injection. The lesson was in front of the agent and the
  mistake was recorded again, so this is a miss, not a catch.
- **What can't be seen.** A lesson that silently prevented a mistake leaves no trace.
  The rule writes no audit rows and no injection-outcome rows, and a recall that
  finds nothing isn't logged. So this is "no evidence of value", not proof of none.

**The class rules** (#18730, "Push injection rules for plan and QA agents"). Their
pools are empty or stale:

| Agents | Lesson class | Lessons in pool | Runs, last 30 days |
| --- | --- | --- | --- |
| plan-adversary, plan-adversary-taskless | reviewer-miss | 0, ever | 46 |
| planner; plan-enhancer, plan-enhancer-taskless | fixer-induced-defect | 8, all from 08-18/19 | 11 + 11 |
| qa-reviewer | qa-miss | 1 (07-24) | 1 |

The plan adversary always gets nothing. Planners get the same 8 month-old lessons on
every run.

**Lessons already reach agents without any push.** Ordinary `search_memories` calls
returned review lessons 886 times, covering 212 distinct lessons. The pushed memory
index never surfaced one.

## Q3. What each delivery path costs

**Touched-files rule** (#15560, "Add file-scoped review lesson guardrail"). It is the
most expensive path, in latency, tokens and code.

- It runs inline before every repo mutation or broad read that has file paths. It
  blocks the tool call for up to 2.0 s, and it keeps that 2.0 s wait even though
  #22807 ("Memory recall inline wait of 2 s drops memories under load: raise to 10 s
  (stopgap)") raised the memory recall wait.
- daemon.log shows 167 of these 2 s timeouts from 09-21 to 09-24 (8, 48, 61 and 50
  per day). That is at least 334 s of blocked tool calls in 4 days. The total number
  of calls is unknown, because empty results leave no trace. Session 78794087
  suspected hook-pool starvation on 09-14, before #22708 ("Hook path saturation:
  admission_wait and rule_evaluation up to 13 s at load 6.5") moved optional hook
  context calls off the blocking path.
- In Claude over 30 days it injected 436k characters (about 109k tokens). The mean
  block is 574 characters; per session the median is 2 injections, the 90th
  percentile 10, and the maximum 41. There are more in other CLIs: Codex 35, Grok
  97 and Droid 8 injections.
- Code: the rule template and installed row, plus special cases in
  `hooks/dispatchers/mcp.py:188`, `workflows/engine/mcp_injections.py:34` and
  `workflows/engine/delivery_formatting.py:11`.

**Class rules** (#18730, "Push injection rules for plan and QA agents"). Four rules
fire at turn start for six agent types, limit 3. Each call is cheap, but the content
is empty or stale (Q2). Codex transcripts from 08-24 to 09-24 show 25 class
injections carrying 88 lessons.

**Close-review recall** (#22822, "Allow required review-learning recall in
task-close reviews"). Every close review loads `review-lessons.md` (3,260 bytes) and
the code-review skill's lesson section. Over 287 runs that is about 0.9 MB, roughly
230k tokens, of instructions to call tools the reviewer can't call. Codex reviewers
tried anyway: 46 denied calls in 4 days, each costing a turn. Where recall is
allowed it isn't always fast: one sampled Codex call took 8.9 s.

## Q4. Keep, trim or retire

1. **Retire the touched-files rule** (#15560, "Add file-scoped review lesson
   guardrail"). Delete the template and the installed row. A disabled row stays
   around to be re-examined, so deletion is the smaller end state. Also remove its
   hook-path wiring (the three special cases in Q3). The pull tool
   `recall_review_lessons_for_files` can stay: it costs nothing when unused. Agents
   called it directly only 6 times in Claude from 09-11 to 09-20.
2. **Retire all four class rules** (#18730, "Push injection rules for plan and QA
   agents"). The pull tool `recall_review_lessons_by_class` can stay on the same
   grounds (14 direct Claude calls from 09-11 to 09-20).
3. **Stop telling the close reviewer to use lesson tools.** Drop `review-lessons.md`
   from `required_skills` in `task-close-reviewer.yaml`. Add one sentence to the
   code-review skill's "Review-Learning Hooks" section: it applies to pre-commit
   review, not task-close review. This resolves #22822 ("Allow required
   review-learning recall in task-close reviews") without granting the tools. It
   also supersedes my earlier pick in the #22508 ("[Gobby Feedback - Reviewed
   Findings]") review, which was to allowlist recall. The reason: the skill says to
   record a lesson only after a verified fix or no-fix decision, and a close reviewer
   submits findings before any fix exists. Given Q2, granting recall would add slow
   calls (one sampled call took 8.9 s) with no shown benefit.
4. **Keep the corpus, `record_review_lesson`, and recall in the pre-commit
   code-review flow.** This is a pull step. It runs only when an agent has findings
   in hand and puts lessons into a table the agent must fill in. Be clear that it has
   been nearly idle for 3 days: 0 Claude recalls since 09-21, and Codex recalls that
   returned results fell from 49 on 09-21 to 5 in total over 09-22 to 09-24. Keep
   it because an unused pull step costs nothing, and apply a retirement condition at
   a one-month check. If pre-commit
   reviews have recorded almost no lessons, and their finding tables' memory column
   is almost always empty, retire recall and recording and keep the corpus only as
   searchable memories. Making this measurable is the same problem as the decision
   ledger in my GitHub trending report, §1 (record what was offered before the
   outcome is known, and attach the outcome later). I am not proposing to build it
   here.
5. **One more item:** the CodeRabbit skill is still bundled
   (`skills/coderabbit/SKILL.md`), and it also tells agents to call
   `recall_review_context`. Retire it if CodeRabbit is gone for good.

Net mechanism after this: two optional pull tools and one skill step, with no
hook-path lesson code.

## Limits

- Claude transcripts are kept for 30 days, so the injection analysis starts on 08-24.
  Grok injection events have no dates.
- "Uptake" means the pattern id or lesson wording appeared in the next 3 assistant
  messages. An agent can follow a lesson without naming it, so this undercounts
  quiet use. Nothing on disk can count it.
- Call counts are real tool calls: Claude `call_tool` uses with a matching
  `tool_name`, and Codex exec calls matched to their outputs. Schema fetches, shell
  text and plan text that merely name a tool are excluded. An earlier draft counted
  those too and overstated Claude calls by about 3 to 4 times.
- Run counts come from `agent_runs`; lesson counts come from memory tags.

## Sources

- Memory table: memories tagged `review-lesson`, split by `source:*`,
  `source-kind:*`, `lesson-type:*` and creation date (read-only queries).
- Claude transcripts, `~/.claude/projects/*/*.jsonl` and subagent transcripts (08-24
  to 09-24): `hook_additional_context` attachments holding `<review-guidance>` blocks,
  the next 3 assistant messages, and `call_tool` uses of the lesson tools.
- Codex rollouts, `~/.codex/sessions/2026/08/24` to `2026/09/24`: injected blocks, and
  `exec` calls holding `call_tool(... tool_name: "recall_review_context" |
  "record_review_lesson")` with their outputs.
- `~/.gobby/logs/daemon.log`: inline timeouts for `recall_review_lessons_for_files`.
- Code: `workflows/rules/review-learning/*.yaml`, `review_learning/service.py:186-227`
  and `:519-569`, `agents/task-close-reviewer.yaml:163` and `:238-246`,
  `skills/code-review/SKILL.md:154-164`,
  `skills/gobby/references/memory/review-lessons.md`.
- Commits: af85a1c9a4 (#22166, "Replace code-reviewer skill with code-review skill
  wrapping ocr delegate mode and gate first commit on it", 09-11); 541320efc2
  (#22636, "Implement OCR-assisted queued task close reviewer", 09-20); 056423f007
  (#15560, "Add file-scoped review lesson guardrail", 06-02); e4bcbf7716 (#18730,
  "Push injection rules for plan and QA agents", 07-23); 6cdbdd3213 (#22807, "Memory
  recall inline wait of 2 s drops memories under load: raise to 10 s (stopgap)").
