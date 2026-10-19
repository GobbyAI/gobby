# Pinned upstream fixture

The `.js.gz` fixtures contain the exact distributed files from
`@anthropic-ai/sandbox-runtime@0.0.79`, under the adjacent Apache 2.0 license.
Deterministic gzip (`mtime=0`) preserves upstream bytes, including EOF formatting.
Decompressed SHA-256:

- `http-proxy-0.0.79.js.gz`: `edd5ad21903b9a9b4d091618c55e7e9bee7f6f5135cc58be668c0a5e6deae582`
- `mux-proxy-0.0.79.js.gz`: `07d6eb4bab65e9abcf6e52f45216903cc82cba890139a149a771733b6f50088a`
- `which-0.0.79.js.gz` (`dist/utils/which.js`): `150f2f55dbb6e74a22a3638732a26bb017dc2559ba6462c630b62d6b0d509076`

Fake SRT installations copy these bytes and apply the same staging patch as the
installer. This keeps integrity tests hermetic while checking the real pinned file.

`settings-loader-0.0.79.tar.gz` contains the exact SRT settings parser, its local
module dependencies, and the manager (used only to extract its Mach argument
path). The real parser imports the included `zod/v3` from `zod@3.25.76`, an
upstream-compatible dependency. Both packages' original licenses are included.
No manager initialization, proxies or sandbox launch runs in this test.

Sources: `https://registry.npmjs.org/@anthropic-ai/sandbox-runtime/-/sandbox-runtime-0.0.79.tgz`
and `https://registry.npmjs.org/zod/-/zod-3.25.76.tgz`. Archive entries are sorted
with zero timestamps and gzip mtime=0. Archive SHA-256:
`c8b6152f8514389aa667948df67913edba8bf0592df839ed553a7fd1f5aa2925`.
Pinned files match the Orchestrator's installed 0.0.79 evidence:

- `dist/sandbox/sandbox-config.js`: `4820e16666ec459592d3b0c0de2b8311b0d3c30eeefc60b9ebb88484c7d2bf7f`
- `dist/sandbox/sandbox-manager.js`: `7b7e3423610eabc43ec3b96fa4c3baa32072baedab7a86447851a95491d29aa3`
