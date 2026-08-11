# Wiki Revisions — Implementation and Planning Order

Ordering checklist for epic **#19670 — Build daemon-native repository
intelligence with gcode and gwiki**, derived from the recorded dependency graph
as of 2026-08-11 (after the dependency re-anchoring: #19664 and #17678 freed
from the epic-level #18902 gate; #19768 ← #17678 and #19769 ← #20005 added to
preserve the real cross-epic constraints at phase level).

Three lanes run in parallel. Order inside a lane is strict; gates between lanes
are marked with **GATE**. A planning track runs alongside so no lane ever
starves waiting for expansion.

**Dispatchable today:** #20009 (Lane A), #18786 (Lane B), #19773 (Lane C).

---

## Lane A — Daemon-native grant chain (#18902, strictly serial)

- [ ] 1. #20009 Fencing epoch and lease identity unification (P1)
- [ ] 2. #20010 v2 grant bundle: schema, signing, and rejection matrix (P1)
- [ ] 3. #20011 Handshake endpoint and interactive principals (P1)
- [ ] 4. #20012 gcore grant client: handshake, cache, renewal, typed errors (P2)
- [ ] 5. #20013 Gate service construction; collapse DSN resolution (P2)
- [ ] 6. #20014 Collapse gcore AI routing to daemon-only (P3)
- [ ] 7. #20015 Remove CLI routing surfaces; bump contracts; deterministic
      outline (P3) — **closing P3 (#20005) unblocks wiki P4 (Lane C step 4)**
- [ ] 8. #20016 Remove standalone mode and local credential ownership (P4)
- [ ] 9. #20017 Remove MemoryWikiStore and daemon-optional wiki modes (P4)
- [ ] 10. #20018 Bind identity on daemon AI and broker routes (P5)
- [ ] 11. #20019 Boundary end-to-end suite (P6) — closes #18902, unblocking
      #19671 execution and #19672 execution

Churn note: P2 (#20012/#20013) rewires gcode service construction and DSN
resolution in the same crates Lane B touches. Whichever side lands second
rebases.

## Lane B — gcode surfaces (#17678, one agent lane, serial)

- [ ] 1. #18786 Fix scoped graph-edge starvation (bug; do first — its query
      shape is what #17680's view and integration tests sit on)
- [ ] 2. #17680 Add class-hierarchy graph support (completes the cross-cutting
      scoped-retention assertions for call + import + inheritance edges)
- [ ] 3. Tasks from the #17678 epic plan (see Planning track item 1): typed
      code-fact contracts, persisted symbol summaries with invalidation and
      regeneration, tagged-comment spans (`NOTE:`/`WHY:`/`HACK:`), facade
      widening and `codewiki_facts` seed retirement (obligations D5.1, D6.1)
- [ ] 4. Close epic #17678 — **GATE: wiki P3 (Lane C step 3) waits on the
      epic closing, which includes the planned remainder, not just the two
      leaves**

## Lane C — Wiki redesign (#19664, phases serial; leaves within a phase per
their recorded deps)

- [ ] 1. P1 (#19766): #19773 Author the wiki output contract →
      #19774 Add the page-type manifest template
- [ ] 2. P2 (#19767): #19775 stable identities and build manifest ·
      #19777 load/validate page-type manifest · #19778 deterministic module
      clustering · #19776 change classification and invalidation ·
      #19779 external-source information model
- [ ] 3. **GATE: #17678 closed (Lane B step 4).** P3 (#19768): #19780 landing
      page · #19781 layers and architecture map · #19782 module scaffolds ·
      #19783 catalog reconciliation · #19784 deterministic projections
- [ ] 4. **GATE: #20005 closed (Lane A step 7).** P4 (#19769): #19785 semantic
      cluster naming · #19786 grounded narrative and concept pages ·
      #19787 guided tours
- [ ] 5. P5 (#19770): #19788 typed graph projection · #19789 insight report ·
      #19790 bounded diagram fallback
- [ ] 6. P6 (#19771): #19791 compact summaries in retrieval/injection ·
      #19792 engine entrypoint assembly
- [ ] 7. P7 (#19772): #19793 full-engine end-to-end acceptance — closes #19664

## Planning track (run alongside the lanes, in this order)

- [ ] 1. **Author the #17678 epic plan — now.** Long pole for Lane C: wiki P3
      gates on the whole epic closing. Absorb #18786 and #17680 into the
      plan's manifest as covering their sections; do not recreate or delete
      them.
- [ ] 2. **Resolve the #19671 Model A/B decision**
      (`.gobby/handoff-session-wiki-scope.md`, cross-project session-wiki
      scope) and author the #19671 ingestion plan. The decision needs no code
      landed; execution waits on #18902.
- [ ] 3. **Plan #19672** (agent-native exploration) once #19773's output
      contract exists — the replacement surface builds on it.
- [ ] 4. **Plan #19665** (orchestration) after wiki P1–P2 land — it maps the
      engine's identity/invalidation contracts onto agents, pipelines, and
      cron, so planning it earlier means guessing.
- [ ] 5. **Plan #18869** (guided exploration) after wiki P3 shapes the
      artifacts it renders. Lowest urgency: it blocks nothing downstream.

## Endgame (execution order after the lanes converge)

- [ ] 1. #19671 Operationalize daemon-native multimodal wiki ingestion
      (after #18902)
- [ ] 2. #19672 Replace wiki ask and research with agent-native exploration
      (after #18902 and #19664)
- [ ] 3. #19665 Orchestrate wiki generation and maintenance
      (after #17678, #19664, and #19671)
- [ ] 4. #18790 Back up, wipe, regenerate, validate, and activate the wiki —
      terminal gate of the whole program (after #19665 and #19672; closes
      #18779)
- [ ] 5. #18869 Deliver guided repository and knowledge exploration
      (after #17678 and #19664; does not block the cutover, can land after it)
