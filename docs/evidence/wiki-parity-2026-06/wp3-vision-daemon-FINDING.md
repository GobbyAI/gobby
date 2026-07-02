# WP3 finding — daemon vision returns empty content, recorded as "extracted"

Date: 2026-06-12 (session #1262)

## Test
`gwiki ingest-file logo.png --topic vision-smoke --vision-routing daemon`
(logo.png = 1024x1024 RGBA PNG, the gobby-cli project logo — real, describable content.)
Ran while the codewiki regen was concurrently loading the daemon text-gen lane.

## Observed
Ingest reported `status: ingested` in ~2.3s. The derived source page
(`wp3-vision-daemon-EMPTY.md`) frontmatter claims success:

```
vision_status: extracted
vision_model: haiku
image_bytes: "173211"
```

…but the `## Vision Description` section is **empty**. The daemon returned a
model name (`haiku`) with **empty content**, and gwiki recorded it as
`extracted` rather than degrading.

## Two distinct issues
1. **Daemon side**: vision via daemon→haiku returned empty content. The
   text-gen fix in the last two daemon commits touched `/api/llm/generate`;
   the vision path posts to `{api_base}/v1/chat/completions`
   (`gcore::ai::vision::describe_image`), a different endpoint — so the fix may
   not cover it. CONFOUND: the request ran under concurrent codewiki load, so
   this may be the #17073 lane-starvation manifesting as empty-content (text-gen
   under the same load fails loudly with "error sending request"). Needs a clean
   re-test with the daemon idle (post-regen) to distinguish daemon-vision-bug
   from load-induced-empty.
2. **gwiki side**: empty vision content is recorded as `vision_status:
   extracted` instead of a degradation marker. This is the same honesty class
   WP2b fixed for the explainer (empty text → Failed). gwiki should treat an
   empty vision description as degraded (`vision_status: unavailable` +
   `degraded: true`), never silently "extracted". Reproducible regardless of
   the daemon root cause.

## Next
- Clean re-test post-regen (daemon idle) to isolate daemon vs load.
- File: gwiki empty-vision honesty gap (gobby-cli); daemon vision-empty (gobby)
  if the clean re-test still returns empty.
