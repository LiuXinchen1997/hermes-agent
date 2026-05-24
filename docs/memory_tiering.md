# Tiered Memory (experimental)

Opt-in three-tier memory store with per-turn semantic recall. Replaces the
flat `MEMORY.md` model when enabled via config flag.

**Status**: experimental. Default off. No behavior change unless you enable it.

## What changes when enabled

| | Default `MemoryStore` | `TieredMemoryStore` (this) |
|---|---|---|
| `target="user"` (USER.md) | unchanged | unchanged |
| `target="memory"` storage | flat `MEMORY.md`, ~2200 char hard cap | `WORKING.jsonl` (T1, ~3000 char soft cap) + `COLD.jsonl` (T2, unlimited) |
| Memory in system prompt | full snapshot, every turn | empty — see "recall" below |
| Per-turn injection | none | top-K relevant entries → `<recalled-memory>` block in user message |
| Over-limit behavior | `add()` returns error | gracefully demote lowest-score entry to T2 |

## When to enable

Useful if you hit any of these:

- `memory.add(...)` keeps returning "exceeds limit"
- `MEMORY.md` accumulates 6-month-old facts that aren't useful anymore but waste prompt budget
- You want the agent to "remember" things that don't all fit into 2200 chars

Skip it if:

- You're happy with the current behavior
- You can't install local embedding models (cluster, restricted env) — the Jaccard fallback works but recall quality drops significantly

## Enable

```bash
# 1. Install the local embedding extra
pip install 'hermes-agent[memory-embeddings]'

# 2. Turn on tiering in ~/.hermes/config.yaml
```

```yaml
memory:
  memory_enabled: true        # existing; required
  user_profile_enabled: true  # existing; optional
  tiering:
    enabled: true             # ← the switch
```

That's the minimum. All other tiering keys have defaults:

```yaml
memory:
  tiering:
    enabled: true
    t1_char_limit: 3000        # T1 soft cap; over this triggers eviction
    tau_days: 14.0             # recency decay constant
    beta: 0.2                  # recency weight in eviction score
    gamma: 0.1                 # frequency weight in eviction score
    prefer_local: true         # try fastembed BGE; false skips straight to keyword
    metrics_enabled: true      # write per-event log to logs/memory_metrics.jsonl
    retrieval:
      enabled: true            # per-turn recall + <recalled-memory> injection
      alpha: 0.7               # cosine weight
      beta: 0.2                # recency weight
      gamma: 0.1               # frequency weight
      tau_days: 14.0
      min_similarity: 0.5      # BGE: 0.5; Jaccard fallback: try 0.1
      k: 5
```

## How recall works

Each turn, before the user message goes to the model:

1. Embed the user message with BGE-small-zh (or fall back to keyword)
2. Score every T1 entry: `α·cosine + β·recency + γ·log1p(recall_count)`
3. Drop anything below `min_similarity`
4. Take top-K (default 5)
5. Render to a `<recalled-memory>` block, prepend to user message
6. Bump `recall_count` and `last_recalled_at` on hits (persisted to jsonl)

The block looks like:

```
<recalled-memory>
[System note: the following are recalled memory entries, not current user input.]
- (3d ago) user prefers pnpm over npm
- (2w ago) project v2.0 release end of June
</recalled-memory>
```

The block goes into the **API-bound** message only, not into `persist_user_message` —
so your conversation history stays clean. Future turns recall fresh against the
actual user words, not against past recall.

## Eviction

When `add()` would put T1 over `t1_char_limit`:

1. Pick the entry with the lowest score (`β·recency + γ·log1p(recall_count)` — no
   cosine since we're not in query context)
2. Demote it to `COLD.jsonl` (append-only, the entry keeps its embedding)
3. Repeat until under limit
4. The just-added entry is **protected** from eviction even if it pushes the
   buffer over

T2 entries are not auto-injected. They can still be hit via `session_search`.
There is no automatic "promote T2 back to T1" path in this version.

## Migration from legacy MEMORY.md

On first `load_from_disk()` with tiering enabled:

- If `WORKING.jsonl` already exists → use it, ignore `MEMORY.md`
- If `WORKING.jsonl` missing and `MEMORY.md` exists → one-shot migrate every
  `§`-separated entry into a T1 jsonl row, leave `MEMORY.md` on disk untouched
  as a fallback

Migration is idempotent on `WORKING.jsonl` presence. If you delete
`WORKING.jsonl` manually, the next load will re-migrate.

## Operational telemetry

`metrics_enabled: true` (default) appends one jsonl event per memory operation
to `~/.hermes/logs/memory_metrics.jsonl`:

```jsonl
{"ts":"...","event":"add","session_id":"...","t1_count_after":12,"t1_chars_after":1850,"evicted":0}
{"ts":"...","event":"recall","session_id":"...","k_requested":5,"k_returned":3,"candidate_count":15,"min_similarity":0.5,"top_score":0.71,"top_similarity":0.68}
{"ts":"...","event":"evict","session_id":"...","victim_id":"...","victim_age_days":28.4,"victim_recall_count":0}
```

Use these to evaluate whether tiering is actually helping. The metrics file
is append-only and untruncated — rotate or clean it manually if it grows.

## Failure modes

| Failure | Behavior |
|---|---|
| `fastembed` not installed | Auto-fall back to Jaccard keyword similarity. Quality drops; consider lowering `min_similarity` to ~0.1. |
| `fastembed` install fails to load model (no network on first run) | Same fallback. |
| `WORKING.jsonl` has malformed lines (manually edited, partial write) | Skip the bad lines, log a warning, continue with the good ones. |
| Whole tiering subsystem fails to init for any reason | `agent_init` falls back to the legacy flat `MemoryStore`. Agent always starts. |
| Recall throws (embedder crashes mid-turn) | Caught, logged at debug; the turn proceeds without recall. |

## Known limitations (not bugs)

- **Single-process file lock not enforced**: if multiple gateway processes write
  to the same `WORKING.jsonl` concurrently, there's a small race window. Not
  handled in this version.
- **`fsync` on every write**: each `add` / `replace` / `remove` blocks on disk
  sync. Fine for typical use; not optimized for high-write-rate scenarios.
- **No `T2 → T1` restore API**: if you want a cold entry back, re-`add` it.
- **`bge-small-zh` is used for all content**: works for both Chinese and English
  but is best on Chinese. Mixed-language workloads may see English-only entries
  match noisier than expected.
- **Multi-modal user messages skip recall**: if the user message is a list
  (images + text), recall is silently skipped.

## Disabling

Set `memory.tiering.enabled: false` (or remove the key) and restart. The flat
`MemoryStore` will be used. Your `WORKING.jsonl` and `COLD.jsonl` files are
preserved on disk; they're just not read. `MEMORY.md` (kept as a migration
fallback) is what the flat store will use.
