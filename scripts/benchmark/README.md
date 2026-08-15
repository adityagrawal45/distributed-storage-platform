# Phase 7 Benchmarking — Cache On vs Cache Off

`benchmark_cache.py` measures end-to-end HTTP latency for the three read
endpoints Phase 7 actually caches, so you can see what caching buys **in
your environment**.

> **No benchmark numbers are published in this repository.** Not in this
> file, not in `README.md`, not in `docs/PHASE_7_REDIS_DESIGN.md`. A
> speedup figure measured on someone else's laptop, against a different
> Postgres, with a different working-set size, is worse than no figure at
> all. This script exists so you can produce your own.

## What it measures

| Endpoint | Why it's in the set |
|---|---|
| `GET /folders/{id}` | One indexed row lookup — the cheapest cacheable read. Shows the **floor**: how much of the response time is network + serialization that a cache cannot remove. |
| `GET /folders/breadcrumb?folder_id=` | The uncached path issues **one query per ancestor** (see `FolderService.get_breadcrumb`'s parent-walk loop). Deep trees are where caching should show the largest relative win — `--depth` controls this directly. |
| `GET /metadata/search?q=` | A filtered query **plus a COUNT**. Two round trips to Postgres collapsed into one Redis GET. |

Reported per endpoint: `n`, `mean`, `p50`, `p90`, `p99`, `min`, `max`
(milliseconds). Percentiles are nearest-rank — every reported value is an
actually-observed request, never an interpolation between two.

## Prerequisites

```bash
pip install httpx          # the only extra dependency
```

A **running** NimbusFS instance with a real PostgreSQL and a real Redis
behind it. `docker compose up -d postgres redis` gets both locally.
Benchmarking against SQLite would measure the wrong database entirely.

## Running the A/B

`CACHE_ENABLED` is read once per process (`get_settings()` is
`lru_cache`d), so the two arms need two server starts. This is
deliberate — a benchmark that flipped the flag mid-run would be measuring
a half-warm process.

```bash
# --- Arm 1: cache ON -------------------------------------------------
CACHE_ENABLED=true RATE_LIMIT_ENABLED=false \
  uvicorn app.main:app --port 8000

# in another terminal
python scripts/benchmark/benchmark_cache.py \
  --base-url http://localhost:8000 --label cached --out cached.json

# --- Arm 2: cache OFF ------------------------------------------------
# (stop the server, restart it with the flag flipped)
CACHE_ENABLED=false RATE_LIMIT_ENABLED=false \
  uvicorn app.main:app --port 8000

python scripts/benchmark/benchmark_cache.py \
  --base-url http://localhost:8000 --label uncached --out uncached.json

# --- Compare ---------------------------------------------------------
python scripts/benchmark/benchmark_cache.py --compare cached.json uncached.json
```

`RATE_LIMIT_ENABLED=false` is not optional in practice: 200 iterations
across three endpoints will trip the default `METADATA` (300/60s) and
`SEARCH` (60/60s) budgets. The script detects a `429` and aborts loudly
rather than quietly reporting the (very fast) rejection latency as if it
were a real read.

### Useful flags

| Flag | Default | Effect |
|---|---|---|
| `--iterations` | 200 | Samples per endpoint. Raise it for stable p99s. |
| `--warmup` | 10 | Discarded requests before measuring. Removes connection setup, SQLAlchemy statement compilation, and the cached arm's one-time population miss from the numbers. |
| `--depth` | 8 | Folder nesting depth. The single biggest lever on the breadcrumb result — try 2 and 20 to see the shape of the win. |
| `--label` | `run` | Name shown in the comparison table. |

## What NOT to conclude

- **"The cache is only X% faster, so it isn't worth it."** This script
  measures latency at **concurrency 1**. A cache's primary job is
  *shedding load* from Postgres, and that benefit only appears under
  concurrency. Run `scripts/load-test/` alongside it, watch Postgres's
  `pg_stat_activity` / connection count with the cache on and off, and
  judge on both numbers.
- **"p50 barely moved."** Look at p99. Caches usually compress the tail
  long before they move the median, and the tail is what users
  experience as "the app is slow".
- **"These numbers will hold in production."** They will not. Production
  has a cold cache after every deploy, a working set far larger than
  what this script seeds, network hops to Memorystore and Cloud SQL, and
  neighbours competing for the same Redis. Treat local results as a
  directional signal about the *shape* of the win, not its magnitude.
- **"Caching made writes slower."** This script does not measure writes.
  Invalidation does add work to every mutation (a bounded `SCAN` +
  `DEL`); if that matters to you, measure it separately.

## Related

- `scripts/load-test/` — Phase 6's k6/Locust chunked-upload load tests.
  Neither those nor this script were executed in the session that wrote
  them; no infrastructure was available. They are runnable, not run.
- `docs/PHASE_7_REDIS_DESIGN.md` — the design these endpoints implement,
  including the failure-scenario analysis worth re-reading before you
  interpret a surprising benchmark result.
