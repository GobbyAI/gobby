# System Requirements

Gobby is a local-first daemon. The runtime footprint depends on which optional components you enable: the daemon alone is light, but the full retrieval stack (PostgreSQL + pg_search, Qdrant, FalkorDB) plus a local embedding model in LM Studio wants modern hardware.

This guide covers what to install and what hardware to plan for, broken out by platform and by component.

## Quick Start

| Platform | Minimum | Recommended |
|----------|---------|-------------|
| **macOS** | Apple Silicon, macOS 14+, 16 GB RAM, Docker Desktop | M3 / M4 Mac mini class, 32 GB unified memory, 8+ cores, 50+ GB SSD |
| **Windows** | Windows 11 23H2+ or Windows 10 22H2 Pro/Edu/Enterprise, WSL 2.1.5+, virtualization/SLAT, AVX2 x64, 16 GB RAM | Windows 11, 32 GB RAM, 8+ cores, 4+ GB discrete VRAM |
| **Linux** | Ubuntu 22.04/24.04 LTS (or equivalent), Docker Engine + Compose, x86_64 or arm64, AVX2 on x64, 16 GB RAM | Ubuntu 22.04 LTS, 32 GB RAM, 8+ cores, NVMe SSD |

The minimums are the realistic floor for running the full stack locally — daemon, Postgres, Qdrant, FalkorDB, and LM Studio with `nomic-embed-text-v1.5`. If you only want the Gobby daemon talking to cloud LLMs, your machine is almost certainly already fine.

## Components

A full Gobby install runs as **one daemon plus a Docker Compose stack of three datastores**, with an optional local embedding server:

| Component | Where | Purpose |
|-----------|-------|---------|
| Gobby daemon | Native (Python 3.13+) | Sessions, tasks, hooks, MCP proxy, rules |
| PostgreSQL + pg_search | Docker (`paradedb/paradedb`) | Relational + BM25 full-text search |
| Qdrant | Docker (`qdrant/qdrant`) | Vector search |
| FalkorDB | Docker (`falkordb/falkordb`) | Knowledge graph (GraphRAG) |
| LM Studio + nomic-embed-text-v1.5 | Native (optional) | Local embeddings; replaceable with Ollama or a hosted provider |

We ship the datastores as separate containers in a Compose file rather than one all-in-one image: upgrades, volumes, resource limits, and crash isolation all stay clean that way.

## Daemon

The daemon itself is modest: a Python 3.13 process, a SQLite database, a FastAPI HTTP server, and a WebSocket server.

| Resource | Requirement |
|----------|-------------|
| Python | 3.13+ |
| RAM | 512 MB idle, 1–2 GB under load |
| CPU | 1 core idle; benefits from concurrency under heavy hook traffic |
| Disk | 1 GB for code + dependencies, plus database growth |

OS support: macOS 14+, Linux (Ubuntu 20.04+ or equivalent), Windows 10/11.

## Backing Stack (Docker Compose)

All three datastores publish multi-arch images (amd64 + arm64), so Apple Silicon runs them natively.

| Service | Image | Notes |
|---------|-------|-------|
| PostgreSQL + pg_search | `paradedb/paradedb` | PG18 with pg_search preinstalled. Set `shm_size: 1g` in compose. |
| Qdrant | `qdrant/qdrant` | POSIX block storage; SSD/NVMe when vectors offload to disk. |
| FalkorDB | `falkordb/falkordb` | Bundled with Redis 7.4. For production, use the server-only image variant. |

### Memory budgeting

There is no fixed minimum — usage scales with your data. Working numbers for a small/dev deployment:

| Service | Idle | Working | Notes |
|---------|------|---------|-------|
| PostgreSQL + pg_search | ~200 MB | 1–2 GB recommended | BM25 index size grows with text corpus. |
| Qdrant | ~150 MB | scales with vectors | Memory ≈ `vectors × dimensions × 4 bytes × 1.5`. For nomic-embed-text-v1.5 (768d), that's **~4.6 GB per 1M vectors** in RAM. Quantization cuts 4–40×. |
| FalkorDB | ~100 MB | 1–2 GB min, 4–8 GB for larger graphs | Use the [size calculator](https://www.falkordb.com/graph-database-graph-size-calculator/) to estimate from nodes/edges/properties. |

For Docker Desktop (Mac/Windows): allocate at least 12 GB to the VM for the floor tier, 16 GB for recommended. Docker Desktop's own floor is 8 GB but that's not enough headroom once all three containers are running. On Linux, Docker Engine has no allocation step — the host's RAM is available directly.

## Local Embeddings (LM Studio)

The default local embedding model is `nomic-embed-text-v1.5` — 0.1B parameters, 768-dim by default with Matryoshka support down to 64-dim if you want to trade recall for footprint.

| Resource | Requirement |
|----------|-------------|
| LM Studio | macOS 14+ (Apple Silicon only), Windows 10/11 (AVX2 x64 or Snapdragon X), Ubuntu 20.04+ (AVX2 x64 or arm64 AppImage) |
| RAM | 16 GB recommended for LM Studio overall |
| VRAM | 0.3–0.8 GB for nomic-embed-text-v1.5 (Q8 → fp16) |
| GPU | Optional. Apple Silicon uses unified memory; on Win/Linux a discrete 4+ GB GPU is recommended for any larger local LLM you load alongside |

The embedding model itself is tiny (~262 MB at fp16). Most of LM Studio's RAM appetite comes from any chat model you load alongside it. LM Studio explicitly does **not** support Intel Macs.

If you'd rather not run LM Studio, swap in Ollama or any OpenAI-compatible embedding endpoint via [search.md](search.md) — Gobby doesn't care which provider serves the embeddings.

## Tested platforms

Floor-tier confirmation we've actually shipped on:

- **Apple Silicon M3** — full stack runs comfortably
- **Apple M4 Mac mini** — full stack runs comfortably

If you have a unified-memory machine in a higher tier, you get more headroom — useful when you want to run a local 30B–70B chat model alongside the embedding model and the Docker stack:

- **Apple M5 / M5 Pro / M5 Max** — up to 128 GB unified memory, 460–614 GB/s on M5 Max
- **AMD Ryzen AI Max+ 395 (Strix Halo)** — up to 128 GB unified memory, up to 96 GB assignable as VRAM. Native Linux is the sweet spot

Neither is required.

## Best Practices

### Do

- Use SSD/NVMe storage. SQLite, Postgres, and Qdrant all hate spinning disks.
- Run the datastores via Docker Compose with one service per container. Keeps upgrades, volumes, resource limits, and crash isolation clean.
- Set Docker Desktop memory allocation explicitly (12–16 GB) rather than letting it default. Default is half of host RAM, which often isn't enough.
- Quantize Qdrant vectors when the collection grows past a few hundred thousand entries — scalar quantization is usually transparent; binary quantization is dramatic.
- On Apple Silicon, prefer arm64 images. All our defaults are multi-arch, so you don't need pinned tags.
- On Linux servers, prefer Docker Engine + Compose over Docker Desktop. Lower overhead, no VM tax.

### Don't

- Run the full stack on 8 GB of RAM. The daemon alone is fine; the Docker side will thrash.
- Mount Postgres or Qdrant data directories on macOS bind mounts for production-scale data — use named volumes for performance.
- Allocate all your host RAM to Docker Desktop. Leave at least 4 GB for the host OS.
- Try to install LM Studio on an Intel Mac. It will not run.

## Troubleshooting

### Postgres "could not resize shared memory segment"

Docker's default `shm_size` is 64 MB. Postgres needs more. Set `shm_size: 1g` in your compose file.

### Qdrant OOM under load

Either (a) enable scalar/binary quantization, or (b) switch to memory-mapped storage for vectors and the HNSW graph. The latter trades latency for footprint.

### LM Studio won't start on Linux/Windows

Verify your CPU has AVX2 (`grep avx2 /proc/cpuinfo` on Linux). Most CPUs from the past decade do, but pre-2013 hardware may not.

### Docker Desktop won't start on Windows

Confirm WSL 2.1.5+ is installed (`wsl --version`) and that virtualization/SLAT is enabled in your BIOS. Docker Desktop on Windows runs containers through the WSL2 backend by default.

### Apple Silicon: "image platform mismatch"

All three backing-stack images are multi-arch — you should not see this for our defaults. If you do, you've pulled an `:amd64`-pinned tag. Use the unpinned tag and Docker will pick arm64.

## See Also

- [configuration.md](configuration.md) — Daemon and project config
- [search.md](search.md) — Search and embedding configuration
- [memory.md](memory.md) — Memory backend configuration
- [CONTRIBUTING.md](../../CONTRIBUTING.md) — Development environment setup
