# Pinned upstream fixture

The `.js.gz` fixtures contain the exact distributed files from
`@anthropic-ai/sandbox-runtime@0.0.79`, under the adjacent Apache 2.0 license.
Deterministic gzip (`mtime=0`) preserves upstream bytes, including EOF formatting.
Decompressed SHA-256:

- `http-proxy-0.0.79.js.gz`: `edd5ad21903b9a9b4d091618c55e7e9bee7f6f5135cc58be668c0a5e6deae582`
- `mux-proxy-0.0.79.js.gz`: `07d6eb4bab65e9abcf6e52f45216903cc82cba890139a149a771733b6f50088a`

Fake SRT installations copy these bytes and apply the same staging patch as the
installer. This keeps integrity tests hermetic while checking the real pinned file.
