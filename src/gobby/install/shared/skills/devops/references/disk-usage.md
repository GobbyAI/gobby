# Disk Usage And Space Recovery (macOS, APFS)

Find what fills the disk, measure honestly, and reclaim space by safety tier.

## Three APFS Facts

1. **Volumes share their container's free space.** A per-volume capacity percentage
   misleads; the sealed system volume often reads 99%. Trust `Avail` and
   `diskutil apfs list`.
2. **`du` misses snapshot space.** Local Time Machine snapshots hold purgeable space
   `du` never counts. Check them when `du` and `df` disagree.
3. **`du` over-counts clones.** APFS copy-on-write clones report their full size even
   when blocks are shared, so deleting a cloned tree can free far less than `du` says.
   `dust`, `gdu` and `ncdu` count the same way. Confirm a deletion's real effect with a
   `df` delta.

## Step 1: Measure

```bash
df -h /
diskutil apfs list | head -60
du -hx -d1 ~ 2>/dev/null | sort -h | tail -15
tmutil listlocalsnapshots /
```

## Step 2: Agent-Host Suspects

| Suspect | Measure | Notes |
|---|---|---|
| Build roots (Gobby agent caches, project `target/`) | `du -sh ~/.gobby/cache/* <project>/target 2>/dev/null` | Usually the largest; see `rust-worktree-builds.md` |
| Orphaned build roots | Compare root names with `git worktree list` and `~/.gobby/worktrees/` | Safe once the tree is gone and nothing builds there |
| Leftover compiler cache | `du -sh ~/.gobby/cache/sccache 2>/dev/null` | Agent builds no longer use sccache; regenerable once no `sccache` process runs |
| Worktrees | `du -sh ~/.gobby/worktrees/*` | Never delete one with unlanded work |
| Container VM image | `docker system df` | Grows; shrink by pruning inside, see Step 3 |
| Package caches | `uv cache dir`, `~/.cargo/registry`, `~/Library/Caches` | Regenerable |
| Test temp folders | `$TMPDIR` children from test runs | Regenerable when no test runs |

## Step 3: Container Space

```bash
docker system df
docker volume ls -f dangling=true
```

Pruning images and build cache inside the VM lets OrbStack shrink its image on its own;
Docker Desktop needs its own reclaim step. Named volumes hold project databases even
when no container runs. Never prune a named volume; anonymous hash-named volumes are
scratch, and still need the operator's approval.

## Step 4: Clean Up By Tier

Exhaust the safe tier first. Every deletion needs the operator's approval; report the
measured size and the `df` delta afterwards.

| Tier | Items | How |
|---|---|---|
| Safe, regenerable | Homebrew downloads, pip and uv caches, orphaned build roots, test temp folders | `brew cleanup -s`, `uv cache clean` (blocks while a `uv run` holds the lock), delete verified orphans |
| Needs a decision | Container images and build cache, local snapshots, live build roots | `docker system prune -a`, `sudo tmutil thinlocalsnapshots /`, prune stale executables |
| User data | Named container volumes, worktrees with unlanded work, documents | Never without explicit per-item approval |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Delete freed far less than `du` said | Clones shared the blocks | Use `df` deltas for decisions |
| `du` total far below used space | Snapshots or another volume in the container | `tmutil listlocalsnapshots /`, `diskutil apfs list` |
| `uv cache clean` hangs | A running `uv run` holds the lock | Wait for it, or clean when idle |
| Docker image stays large after prune | Docker Desktop does not auto-shrink | Use its reclaim option, or OrbStack |

_Version: 1.0.1_
_Last verified: 2026-11-07_
