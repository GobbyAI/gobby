# GitHub Trending Weekly #50: what to integrate, steal or build

- **Status:** research, read-only. Nothing was installed, cloned or run.
- **Author:** Researcher gobby#14435, 2026-09-24.
- **Requested by:** #14069, relaying Josh (message 134a041e).

## Request

Josh sent four Telegram messages, about 13:01-13:05 CT, relayed verbatim:

1. "https://youtu.be/GeYevz27gyc?si=io1GOg0WFQDXGegO"
2. "That threw an error, sending you that link"
3. "Those we want to research, see if there's low hanging fruit or long term things we
   could integrate/steal/build our own version"
4. "https://githubawesome.com/github-trending-weekly-50/"

#14069 asked for the findings split into low-hanging and long-term, each marked
integrate, steal the idea, or build our own.

## How this was done, and the limits

- **The page.** The githubawesome page (24 Sep 2026) is paywalled after entry No. 1.
- **The video.** YouTube's page would not render for a fetch. Its oEmbed title is
  "GitHub Trending Weekly #50: openmuse, open-glean, unreal-agent, BongoCat, laya-mlx,
  mini-AGI, flute", from the channel Github Awesome.
- **The full list.** The 35 repos, with timestamps and links, came from a third-party
  video index (videohighlight.com/v/GeYevz27gyc).
- **Reading them.** Four read-only subagents read each repo's GitHub page. I checked the
  claims that drive a recommendation myself (§4).
- **Treat every number below as the repo author's own claim.** Nothing was run. Model
  weights on Hugging Face were not checked, so weight licenses are as the README states
  them, or unknown.
- **Every one of these repos is under two weeks old** (created 12-22 Sep). The star
  counts reflect launch hype, not maturity. For example, laya-mlx has 6.2k stars on
  6 commits.

## Answer

**One real finding.** Six of the 35 repos are about Jev. Several are open,
local-running models in the Jev style, and they compete for the local "Kev" slot Josh
reserved in job 1. That slot is an opt-in installer option after 0.6, with pilots
waiting for a local model.

- **Laya** stands out on paper: Apache-2.0 weights per its README, 322M-421M
  parameters, and about 5-14 ms per decision on Apple Silicon through the Core ML and
  MLX ports. Kev-0.8B takes 149 ms (job 1 report, §2). The machines differ (M3 Max
  and M5) and neither side has been checked, so read that as a rough 10x, not a
  measured result.
- **No work is due now.** #22604 is parked. When it is unparked, benchmark Kev, Laya,
  jeff and reflex on Gobby's own labeled set before choosing.

**Everything else** is one of three things:

- something Gobby already does;
- blocked behind the same parked decision;
- irrelevant.

Restraint rung 1 applies: most of what looks like "steal this" is a mechanism Gobby
already has (§3).

### Low-hanging

| # | Item | Verdict | What to do |
| --- | --- | --- | --- |
| 1 | Local Jev-style decision models: **Laya** (mizorewww/laya-mlx, mizorewww/laya-coreml, receptron/laya), **jeff** (logan-markewich/jeff) and **reflex** (kshetrajna12/reflex) | INTEGRATE as endpoints, when #22604 is unparked | Add them to the Kev comparison in the classification report, and bench them before choosing (§1) |
| 2 | Jevmind's decision ledger and calibration method | STEAL into #22604's spec, when unparked | Details in §1 |
| 3 | flute (webprodigies-org/flute): turns a real React UI into cinematic MP4 camera moves | INTEGRATE as a dev-only tool, if gobby.ai wants demo video | Run it in Docker, per Josh's preference for tools he won't keep (memory a91effbb). Optional. |

### Long-term

| # | Item | Verdict | Condition |
| --- | --- | --- | --- |
| 4 | foremerge (naw103/foremerge): agents declare `symbol:X=replace`-style intent before editing, and conflicts are caught before any code exists | STEAL the symbol-level claim | Only if file-level overlaps start blocking parallel dispatch. Gobby's `DispatchWriteSetGuard` already does this per file, and today's daemon log shows 0 overlap skips (§3). |
| 5 | JevRouter (BillionsBobby/JevRouter): picks a tool, model or agent with one Choice question | STEAL the candidate record and the filter-then-substitute step | Only once a local decision model exists and `recommend_tools` needs it |
| 6 | Pick-from-candidates action grounding: fast-browser-use, arc-cua, FluidUse, jev-voice-browser | STEAL the idea | Only if Gobby builds its own browser or computer-use tool. Today Gobby bundles Playwright and chrome-devtools MCP templates. |
| 7 | ZCode (zai-org/ZCode): Z.ai's coding agent, 6.7k stars, v3.14.3 (23 Sep), Apache-2.0, desktop, web and TUI | Watch as a possible seventh CLI adapter | Only if it gains users and exposes hooks. Its README doesn't document hooks, MCP or subagents. |

## 1. Local decision models, in detail

All of these use TypeSafe's System One shape: `{model, state, questions}`, with
`choice`, `score` and `noul` questions, served at `POST /v1/systemone` or through an
equivalent library call.

That supports job 1's advice: make the #22604 client's endpoint and model id
configurable (classification report §2.3, §4.1). One client then reaches TypeSafe,
OpenRouter and any of the local servers below.

One subagent claimed that OpenRouter's envelope differs and that JevRouter translates
between the two. JevRouter's README says neither, so there is no new evidence against
job 1's finding.

| Model | Runtime and license | Speed (author's claim) | Accuracy (author's claim) | Fit |
| --- | --- | --- | --- | --- |
| Kev (from job 1) | Own server, MLX on a Mac; Apache-2.0 | Kev-0.8B: 149 ms on new text, M5 | Kev-27B 0.848 against Jev's 0.857 | The current leading candidate |
| **Laya** (Convai), mizorewww ports | Python library, MLX or Core ML; code Apache-2.0, weights unverified | 13.4 ms p50 (MLX, M3 Max); 4.98 ms p50 (Core ML on the Neural Engine, but only 96 tokens; 1024 tokens takes about 92 ms) | None published | Fastest by far. It's a library, not a server, so it needs a small `/v1/systemone` wrapper, or someone else's. |
| Laya, receptron port | Node, ONNX; README says weights `convaiinnovations/laya` are Apache-2.0 | About 140 ms on an Apple Silicon CPU, 1.7 GB fp32 | None | Node-only. Input stops at 512 tokens. |
| **jeff** | Python server that works with TypeSafe's SDK unchanged; code MIT, weights license not stated | 151 ms p50, on an L4 GPU (no Mac numbers) | JevBench 66.9 against Jev's 75.3 | Easiest to drop in. Accuracy is clearly lower. |
| **reflex** | Python server at `/v1/systemone`, a frozen 4B model with MPS support; MIT | About 200 ms | Not stated | Asks each question twice with the options in a different order, to reduce position bias |
| nimble (bespokelabsai) | Qwen3.5-9B LoRA on MLX; **no license** (LICENSE file 404s) | Not stated | Synthetic labels; "built in one day" | Skip until it has a license |

**What changes for the Kev slot:**

- Nothing is decided until #22604 is unparked. Josh's rulings stand: no hosted text,
  pilots wait for a local model, opt-in after 0.6.
- Gobby doesn't install or run local models (memory 606b6838). "Integrate" here means
  a configurable endpoint, never bundling a model into the daemon.
- The pilot should compare candidates on Gobby's labeled community labels instead of
  assuming Kev. Laya's speed and small size could matter for high-volume gates, but it
  has no published accuracy.

**Jevmind (dealerdefi/Jevmind): ideas worth writing into #22604's spec.** Its local
backend is rules plus BM25, not a model, and its adapter was tested only against a
stand-in. These ideas don't depend on any particular model:

- **A decision ledger.** Record the state fingerprint, answer, confidence and gate
  action before the outcome is known, then attach the outcome later. This is what
  makes later calibration honest.
- **A time-ordered calibration split.** Fit Platt scaling on the oldest 70% of labeled
  answers, test on the newest 30%, and drop the calibration if it doesn't beat the
  uncalibrated baseline.
- **Uncertainty.** For guard decisions, "unsure is never allow". docjev adds a review
  band: a score within 0.1 of the threshold gets flagged.
- **Replay tapes.** Store answers keyed by state plus questions, so tests and evals run
  deterministically with no model and no network.

## 2. Everything else, repo by repo

| Repo | What it is | Verdict | Why |
| --- | --- | --- | --- |
| CopilotKit/openmuse | Self-hosted personal agent with a browser, a Docker terminal, Gmail and Calendar; alpha | SKIP | Different product. Its "command receipts" and "no silent retry after uncertain external writes" are sound rules, but Gobby already records validation commands from the transcript for close gates. |
| hydra-db/open-glean | Research UI over hosted HydraDB | SKIP | Needs a hosted DB, so data leaves the machine. Ask already merges verified citations. |
| unreallabsai/unreal-agent | Agent harness in Go | SKIP | Overlaps Gobby's session and spawn layer. Its append-only, forkable session store isn't needed next to PostgreSQL sessions. |
| penso/herdr-gpui | GPUI client for the Herdr daemon; the daemon owns terminal state and the client paints frames received over a Unix socket | SKIP (already have it) | gclient uses the same split: `UnixSocketFrameSource` in `crates/gclient/src/frame_source.rs:414`, and `FrameMailbox` in `crates/gterminal/src/host/backpressure.rs:89`. The only new thing is a 160x50, 40-agent render benchmark. |
| yetone/magpie | Model and provider switcher across agent CLIs, plus an API-translation gateway | SKIP | Gobby's Codex installer already edits TOML in place with `tomlkit` (`src/gobby/cli/installers/codex.py:19`). No stated need for cross-CLI profiles or a gateway. |
| ethanplusai/astra-flash-orchestrator | Codex skill: a strong model plans and reviews, a cheap model implements | SKIP | Gobby already does this through `spawn_agent` model choice and coordinator review (memory 6ce52005). |
| OnlistTeam/ai-manager | Tauri app that installs and repairs 15+ AI CLIs | SKIP | AGPL-3.0. The README admits provider credentials are stored in plaintext. |
| witnesstodark/mr-mak-workspace | CLI chats plus a notes workspace; Windows only | SKIP | Its `.agents/skills` directory doesn't matter here, because Gobby serves skills over MCP. |
| isas1/skills | Two small Claude skills | SKIP | Nothing to take |
| volotat/mini-AGI | Toy continual-learning language model | SKIP | Irrelevant |
| APUS-AI-Lab/fast-browser-use | Browser skill: a local Qwen3.5 model picks the next action from the page's own elements, one token per candidate | Long-term item 6 | Timed only on an RTX PRO 6000. Needs 16-32 GB and its own inference stack. |
| shhivv/arc-cua | macOS action layer: accessibility tree plus OCR, with a `{goal, inputs, verification, constraints}` contract that returns `SUBTASK_COMPLETE`, `BLOCKED` or `NEEDS_AGENT` | Long-term item 6 (the contract only) | Sends the accessibility tree and screen text to hosted Jev, which breaks no-hosted-text |
| FluidInference/FluidUse | Swift form-filler that reads the accessibility tree and types through it, with tiny Core ML models; never submits by default | Long-term item 6 | Swift-only, with no IPC to the daemon |
| moritzkremb/jev-voice-browser | Voice control for a browser via Jev | SKIP | Demo; sends the transcript and a page snapshot to TypeSafe |
| jerryjliu/docjev | Classifies and splits document packets via Jev | SKIP | Documents, not code. Always sends page text to hosted Jev. The review-band idea is in §1. |
| driceroland/Search | macOS WebKit browser | SKIP | No AI |
| ejfkdev/ddc | Android DEX-to-Java decompiler | SKIP | Irrelevant |
| surya-koritala/Glyd | Rust compression library | SKIP | New format, and the store component is BUSL-1.1 |
| v-modal/vmodal_sdk_swift_iphoneduo | Client SDK for a paid remote video search | SKIP | Irrelevant. The stars-to-forks ratio (600 to 2) suggests inflated stars. |
| vladelaina/BongoCat | Desktop pet that reacts to input | SKIP | AGPL, and it captures global input. An agent-status mascot is a possible idea, but nobody has asked for one. |
| blixvip/NullMotion | Syncs motion graphics against storyboards | SKIP | No license |
| anishfn/shapeshift | Text input that morphs into cards, driven by an intent classifier | SKIP | A UI idea with no current need |
| sevenevesai/riso-windowseat | Generative risograph art | SKIP | Irrelevant |
| haplollc/ThinkingOrbs | SwiftUI "thinking" indicators, ported from a TS original | SKIP | Two commits and Swift-only. The web UI would need the TS original, and nobody has asked for it. |
| bespokelabsai/nimble | Decision model | SKIP for now | No license (§1) |

## 3. Checks against what Gobby already has

- **Browser tools.** `src/gobby/install/shared/mcp/templates/playwright.yaml`
  (`@playwright/mcp@latest`) and `chrome-devtools.yaml` are bundled. Josh also said
  not to install the Codex Computer Use plugin unless he asks (memory 61c6a4a4).
- **Parallel-edit conflicts.** `src/gobby/dispatch/dispatcher.py:312` skips a task whose
  write set overlaps a running one (`write_set_guard.py`). The live
  `~/.gobby/logs/daemon.log` (6.5 MB) has 0 "Dispatcher skipped overlapping write-set
  task" lines, so there is no evidence yet that file-level granularity is costing
  parallelism. Two caveats: `gobby build` dispatch runs only when Josh authorizes it
  (memory 4644eba6), so the dispatcher may barely have run; and I didn't check which log
  level `_log_write_set_overlap` writes at.
- **Terminal frames.** gclient receives frames over a Unix socket from the gterminal
  host, which applies backpressure. That is the design herdr-gpui advertises.
- **Config editing.** The Codex installer and `agents/trust.py` use `tomlkit` for
  comment-preserving edits.

## 4. Claims I checked myself

- **Dropped:** "OpenRouter's envelope differs from systemone; JevRouter translates."
  Not in JevRouter's README.
- **Corrected my own framing:** my prompt to the browser subagent said Gobby agents have
  no browser tool of Gobby's own. In fact Gobby bundles two browser MCP templates.
- **Corrected:** one subagent rated Laya "INTEGRATE, low-hanging". It is technically
  small, but blocked by #22604 being parked and by Josh's post-0.6 installer ruling.
- **Corrected:** one subagent rated fast-browser-use "INTEGRATE as a trial skill". The
  existing browser MCP templates already cover the capability, and it needs a
  16-32 GB local model.

## Sources

- **The list:**
  - https://githubawesome.com/github-trending-weekly-50/ (paywalled after No. 1)
  - the YouTube oEmbed for GeYevz27gyc
  - https://videohighlight.com/v/GeYevz27gyc (the full list of 35)
- **The repos:** each repo's GitHub page, fetched 2026-09-24 and treated as untrusted.
- **Gobby:**
  - `.gobby/plans/research/classification-decision-models-2026-09.md` (§2, §2.3, §4.1, §5)
  - memories 5d9cd410 (MLX installer direction), 606b6838 (no local model serving),
    a91effbb (Docker for third-party tools), 61c6a4a4, 6ce52005
