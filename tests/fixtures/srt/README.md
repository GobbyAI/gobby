# Pinned upstream fixture

The `.js.gz` fixtures contain the exact distributed files from
`@anthropic-ai/sandbox-runtime@0.0.76`, under the adjacent Apache 2.0 license.
Deterministic gzip (`mtime=0`) preserves upstream bytes, including EOF formatting.
Decompressed SHA-256:

- `http-proxy-0.0.76.js.gz`: `fbf1184b57d41400f5c73b5f0626df901caae0278b7b37103263a587e9c0cda4`
- `mux-proxy-0.0.76.js.gz`: `07d6eb4bab65e9abcf6e52f45216903cc82cba890139a149a771733b6f50088a`

Fake SRT installations copy these bytes and apply the same staging patch as the
installer. This keeps integrity tests hermetic while checking the real pinned file.
