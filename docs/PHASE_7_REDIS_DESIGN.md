# Phase 7 — Distributed Redis Caching & Coordination

**Technical design document.** Companion to `README.md` §14 (which is the
user-facing walkthrough); this file is the engineering rationale, the
race analysis, the failure catalogue, and the interview Q&A.

**Status:** implemented and tested. 246/246 tests pass (145 pre-existing
Phases 1–6, 101 new). No benchmark numbers appear anywhere in this
document — see `scripts/benchmark/README.md` for why, and for how to
produce your own.

---

## 1. Position in the architecture

```
                          ┌──────────────────────────────┐
                          │   Client (browser / SDK)     │
                          └──────────────┬───────────────┘
                                         │ HTTPS
                          ┌──────────────▼───────────────┐
                          │  GCLB + GKE Ingress (Ph. 5)  │
                          └──────────────┬───────────────┘
                                         │
        ┌────────────────────────────────┼────────────────────────────────┐
        │                                │                                │
┌───────▼────────┐              ┌────────▼───────┐              ┌─────────▼──────┐
│  Pod: nimbusfs │              │  Pod: nimbusfs │              │  Pod: nimbusfs │
│  (replica 1)   │              │  (replica 2)   │              │  (replica N)   │
│                │              │                │              │                │
│  RateLimiter ──┼──┐        ┌──┼── RateLimiter  │        ┌─────┼── RateLimiter  │
│  CacheService ─┼┐ │        │ ┌┼── CacheService │        │  ┌──┼── CacheService │
│  DistLock ─────┼┼┐│        │┌┼┼── DistLock     │        │  │┌─┼── DistLock     │
└────────────────┘│││        │││└────────────────┘        │  ││ └────────────────┘
                  │││        │││                          │  ││
                  ▼▼▼        ▼▼▼                          ▼  ▼▼
        ┌─────────────────────────────────────────────────────────────┐
        │        REDIS  /  Cloud Memorystore   (shared, ephemeral)     │
        │  ─────────────────────────────────────────────────────────   │
        │  nimbusfs:user:{id}            nimbusfs:ratelimit:{cat}:{id} │
        │  nimbusfs:folder:{id}[:...]    lock:nimbusfs:lock:cache:...  │
        │  nimbusfs:file:{id}[:...]      idempotency:{user}:{key}      │
        │  nimbusfs:search:{user}:{fp}                                 │
        │                                                              │
        │  ***  NEVER authoritative.  NEVER stores file bytes.  ***    │
        │  ***  Losing all of it costs latency, never data.     ***    │
        └─────────────────────────────────────────────────────────────┘
                  │                                    │
   cache miss ────┘                                    └──── bytes never here
                  │                                                 │
        ┌─────────▼──────────┐                         ┌────────────▼───────────┐
        │ PostgreSQL / Cloud │                         │  Google Cloud Storage  │
        │ SQL — AUTHORITATIVE│                         │  — AUTHORITATIVE for   │
        │ for ALL metadata   │                         │    file content        │
        └────────────────────┘                         └────────────────────────┘
```

The single invariant everything below follows from:

> **Postgres owns metadata. GCS owns bytes. Redis owns nothing.**
> Flushing Redis entirely, at any moment, must cost only latency.

Two direct consequences, enforced in code:

1. `CacheSerializer.encode` **raises** if handed `bytes` — file content
   physically cannot be written to Redis by this codebase.
2. Every `CacheService` method catches every Redis exception, logs it,
   and returns the "as if the cache did not exist" answer. A cache
   failure can degrade performance; it can never fail a request.

---

## 2. Module map

```
app/core/cache/
  keys.py            CacheKeyBuilder    — WHAT a key is called
  serializer.py      CacheSerializer    — HOW a value is encoded
  policy.py          CachePolicy        — HOW LONG a value lives
app/core/
  rate_limiter.py    RateLimiter        — token bucket in atomic Lua
  distributed_lock.py DistributedLock / DistributedLockFactory (Phase 4)
                     + DistributedLockService (Phase 7 facade)
app/services/
  cache_service.py     CacheService     — the ONLY gateway to Redis-as-cache
  cache_invalidator.py CacheInvalidator — operation -> key-set fan-out
app/dependencies/
  rate_limit.py      rate_limit(category) FastAPI dependency + provider
app/middleware/
  rate_limit.py      RateLimitHeadersMiddleware (was the Phase 4 no-op)
```

`redis.asyncio` is imported by exactly three modules: `app/database/redis.py`
(the pool), `app/services/cache_service.py`, and `app/core/rate_limiter.py`.
No route handler, and no other service, talks to Redis directly.

---

## 3. Cache-aside (lazy loading) — and why not the alternatives

```
   READ                                          WRITE
   ────                                          ─────
   ┌──────────────┐                              ┌──────────────┐
   │ GET key      │                              │ UPDATE row   │
   └──────┬───────┘                              │ in Postgres  │
          │                                      └──────┬───────┘
     hit  │  miss                                       │
    ┌─────┴─────┐                                 ┌─────▼──────┐
    │           ▼                                 │ DELETE key │
    │   ┌───────────────┐                         │ (+ related)│
    │   │ SELECT from   │                         └────────────┘
    │   │ Postgres      │                          never "UPDATE key"
    │   └───────┬───────┘                          — see §6
    │           ▼
    │   ┌───────────────┐
    │   │ SET key, TTL  │
    │   └───────┬───────┘
    └─────┬─────┘
          ▼
       return
```

| Strategy | Why not chosen |
|---|---|
| **Write-through** (write DB and cache together) | Every write pays cache-write latency even for data nobody reads again — and most NimbusFS writes (uploads) are never re-read soon. Worse, it is a stale-data source under concurrency: two writers can apply their *cache* updates in the opposite order to their *database* commits, leaving the cache permanently disagreeing with Postgres with no TTL-independent way to notice. |
| **Write-behind** (write cache, flush to DB async) | Makes Redis authoritative for a window. Redis is not durable enough for file metadata, and this violates the phase's core constraint. |
| **Read-through** (cache library owns the DB fetch) | Requires the cache layer to know how to query — inverting the dependency and putting SQL behind the caching abstraction. Cache-aside keeps the loader in the service that owns the domain logic. |
| **Cache-aside** ✅ | The cache is purely opportunistic. It can be empty, stale-then-corrected, or entirely absent, and the system is still correct. Only data actually requested is ever cached. |

---

## 4. Serialization: JSON, explicitly not pickle

Three independent disqualifiers for `pickle`, any one of which suffices:

1. **`pickle.loads` is arbitrary code execution.** The cache is shared,
   network-reachable, and multi-writer. A single Redis compromise, a
   misconfigured NetworkPolicy, or one buggy sibling service writing to a
   colliding key escalates directly to RCE inside every API pod. JSON's
   worst case is a wrong-shaped dict, which the envelope check rejects.
2. **Pickle is not stable across versions.** It encodes fully-qualified
   class paths. Renaming `app.schemas.folder.FolderRead` — a routine
   refactor — makes every entry written by the old build undecodable by
   the new one, *during a rolling deploy where both are live*.
3. **Pickle is Python-only.** A cached entry should be readable with
   `redis-cli GET` during an incident.

### The versioned envelope

```json
{"v": 1, "ts": "2026-08-15T12:00:00+00:00", "d": { ...payload... }}
```

`v` is `CACHE_SCHEMA_VERSION`. A reader that sees a `v` it does not
understand **treats the entry as a cache miss** — falls through to
Postgres, repopulates — rather than raising. This is the property that
makes a cache-format change safe to deploy: the worst it can cost is one
cold period, never an outage. Same for malformed JSON, a non-envelope
value, or a bytes payload that will not decode as UTF-8.

Non-JSON-native types our Pydantic schemas use are encoded on the way out
(`datetime`→ISO-8601, `UUID`→str, `Decimal`→str **not float**, `Enum`→value,
`set`→sorted list, `BaseModel`→`model_dump(mode="json")`) and coerced back
by Pydantic on the way in (`FolderRead.model_validate({...})` accepts all of
those natively). There is deliberately **no decoder hook** that guesses
whether a string "looks like" a datetime — such a hook eventually
mis-coerces a filename.

---

## 5. Cache keys

```
nimbusfs : <entity> : <id> [ : <derived> ] [ : <fingerprint> ]
    │          │        │         │              │
    │          │        │         │              └── SHA-256[:32] of canonical params
    │          │        │         └── children / breadcrumbs / versions
    │          │        └── UUID
    │          └── user | folder | file | search | ratelimit | lock | guard
    └── CACHE_KEY_PREFIX (co-tenancy namespace; SCAN nimbusfs:* is a full inventory)
```

| Key | Entity |
|---|---|
| `nimbusfs:user:{user_id}` | user profile |
| `nimbusfs:folder:{folder_id}` | folder metadata |
| `nimbusfs:folder:{folder_id}:children:{fp}` | children listing, per sort/filter variant |
| `nimbusfs:folder:root:{owner_id}:children:{fp}` | top-level listing (no parent folder to key on) |
| `nimbusfs:folder:{folder_id}:breadcrumbs` | breadcrumb trail |
| `nimbusfs:file:{file_id}` | file metadata |
| `nimbusfs:file:{file_id}:versions` | version history |
| `nimbusfs:search:{owner_id}:{fp}` | one search result page |
| `nimbusfs:ratelimit:{category}:{identity}` | token bucket |
| `nimbusfs:lock:cache:{hash}` | stampede lock (prefixed `lock:` again by `DistributedLock`) |
| `nimbusfs:guard:{hash}` | post-invalidation write tombstone (opt-in) |

**Collision safety.** The entity type is always the second segment, so
`folder:<uuid>` and `file:<uuid>` cannot alias even with identical IDs.
Variable-length or user-supplied components are never interpolated raw —
they are canonicalized (sorted `k=v` pairs) and hashed, which makes keys
fixed-length and removes any chance of a value containing `:` and forging
a different key shape. `None` encodes as `~none`, never `"None"`, so a
folder literally named `None` cannot alias the root.

**Two documented deviations from the spec's key list**, both deliberate:

- `:children` carries a params fingerprint. The listing is parameterized
  (sort field, direction, trash filter); one key for three orderings would
  serve clients the wrong order. Invalidation still targets the
  un-suffixed prefix via `SCAN`, so its semantics are unchanged.
- `search:` puts `owner_id` **before** the hash as a structural segment
  rather than folding it into the hash. This makes "drop all cached
  searches for this user" a single bounded SCAN pattern, and guarantees
  that even a theoretical hash collision cannot leak one user's results to
  another.

---

## 6. TTL strategy

| Entity | Default | Reasoning |
|---|---|---|
| user | 900s | Profile/role/status changes are rare and administrative. |
| folder | 300s | Explicit invalidation is the mechanism; TTL is the backstop. |
| folder children | 300s | Highest-churn folder key (any child create/delete/move changes it) — so it also has the most invalidation call sites. |
| folder breadcrumbs | 300s | Changes only on an *ancestor* rename/move — see the fan-out simplification in §7. |
| file | 300s | Mirrors folder metadata. |
| file versions | 300s | Effectively append-only; invalidated by any version-creating write. |
| search | 90s | A derived view over many rows that cannot be invalidated precisely. Shortest TTL by design. |

TTL here is a **correctness** knob wearing a performance costume: it is the
hard ceiling on staleness if an invalidation is ever missed, dropped, or
raced. That is why there is one per entity, driven by "how bad is N seconds
of staleness for *this thing*", and why no duration is hardcoded at a call
site — all of them come from `Settings.CACHE_TTL_*` via `CachePolicy`.

---

## 7. Invalidation strategy, and the race we accept

### Delete, never update

Every `CacheInvalidator` method issues a `DEL`. None writes a fresh value
in. Deleting is idempotent and order-independent: the loser of any race
just causes one extra read. Updating is neither — see the write-through
row in §3.

### Fan-out per operation

| Operation | Keys cleared |
|---|---|
| folder create / rename / trash / restore / purge | `folder:{id}*` (self + children + breadcrumbs), parent's `children:*` |
| folder move | `folder:{id}*`, **old** parent's `children:*`, **new** parent's `children:*` |
| folder trash / restore (subtree) | the above, **for every descendant individually** — each has its own `is_deleted` flag in its own entry |
| file create / update / rename / trash / restore / purge / new version | `file:{id}*`, containing folder's `children:*`, `search:{owner}:*` |
| file move | the above, plus the destination folder's `children:*` |

Pattern deletes use `SCAN` (cursor-based, incremental), never `KEYS`
(O(N) over the whole keyspace, blocking Redis's single command thread —
a self-inflicted outage on a production instance). `CacheService.scan_keys`
additionally bounds the worst case at 5000 keys per call.

### Race #1 — invalidate-before-commit (accepted, bounded, mitigable)

Invalidation is issued inside the service method, which is inside the
request's transaction (`get_db` commits at the request boundary).

```
  Writer (request A)                    Reader (request B)
  ──────────────────                    ──────────────────
  BEGIN
  UPDATE folder SET name='New'
  DEL nimbusfs:folder:X   ◄── cache now empty
                                        GET nimbusfs:folder:X  -> MISS
                                        SELECT ... -> reads name='Old'
                                                      (A hasn't COMMITted)
                                        SET nimbusfs:folder:X {'Old'}  ◄── STALE
  COMMIT
                                        ...stale entry survives until TTL
```

Three things bound this, stated plainly rather than papered over:

1. The window is the remainder of one transaction — typically
   sub-millisecond, never longer than the request itself.
2. Staleness is capped at the entity TTL (5 minutes default), not
   unbounded.
3. `CACHE_WRITE_GUARD_SECONDS > 0` closes it completely: invalidation also
   plants a short-lived tombstone (`nimbusfs:guard:{hash}`) that makes
   `CacheService.set` refuse to write that key. B's stale write is
   rejected. Cost: one extra `EXISTS` per population, and the key stays
   deliberately cold for the guard duration after *every* write. Off by
   default; implemented and tested (`TestWriteGuard`).

As of the Phase 7 follow-up, `CACHE_WRITE_GUARD_SECONDS` ships **ON by
default at 1.5s** (previously off), so this race is closed in the common
case out of the box — the analysis above and the config knob to disable it
both remain accurate, only the default changed.

**The airtight fix** — invalidating in an `after_commit` hook — requires a
transaction-lifecycle hook the current per-request Unit of Work does not
expose to the service layer. That remains a real, acknowledged limitation
of this phase, not an oversight — the write guard is the pragmatic close,
not the architectural one. It is a clean future addition: register a
SQLAlchemy `after_commit` event on the request's session in `get_db` and
have `CacheInvalidator` queue keys onto it instead of deleting inline.

### Race #2 — ancestor fan-out (fixed: precise invalidation)

Renaming or moving a folder changes the materialized `path` of every
descendant, and therefore every descendant's breadcrumb cache. This used
to be left to TTL as a documented simplification; it is now invalidated
precisely instead.

`FolderRepository.list_descendants(folder, owner_id)` already existed (the
soft-delete cascade in `delete_folder` uses it) — it does one query
matching every folder whose `path` is nested under the target's `path`
prefix. `rename_folder` and `move_folder` now call it themselves, captured
**before** `cascade_rename` mutates `path` (folder IDs are stable across
the rewrite, so a pre-mutation ID set is still exactly the post-mutation
descendant set), and pass the ID list to
`CacheInvalidator.descendant_breadcrumbs_changed()`, which issues one exact
`DEL` per descendant's `breadcrumbs` key — no `SCAN`, no new Redis index.

This adds no new query-complexity class: `cascade_rename` already visits
every descendant row in Postgres for the same rename/move, so gathering
their IDs first is one extra `SELECT` of a set the write was already about
to touch, not a new O(N) cost the operation didn't already have. The
window this doesn't close is the same Race #1 above (the invalidation
happens pre-commit) — it is now subject to the same write-guard mitigation
as every other invalidation, not to an additional five-minute TTL wait on
top.

### Race #3 — search staleness (mitigated coarsely)

A search result's membership can be changed by any file write, and there
is no way to know which cached queries matched without re-running them.
Handled by a bounded per-user `SCAN nimbusfs:search:{owner}:*` + delete on
every file mutation, plus the shortest TTL in the system. Precise fan-out
would require a reverse index from row to query — a search-engine feature,
not a cache feature.

---

## 8. Cache stampede (thundering herd) protection

### The problem

```
   t=0   key expires / is invalidated
   t=0+  500 concurrent requests all GET  -> 500 MISSes
   t=0+  500 identical SELECTs hit Postgres simultaneously
         ...precisely when the system is already under load.
```

### The chosen mitigation: single-flight with a bounded wait

```
 request ──► GET key ──hit──► return                       (hot path: no locking at all)
                │
               miss
                │
                ▼
        SET NX  nimbusfs:lock:cache:{hash}   (TTL 5s)
                │
      ┌─────────┴──────────┐
   won│                    │lost
      ▼                    ▼
  re-GET (double-check)   poll GET every 20ms
      │  hit ──► return    for up to 500ms
     miss                  │
      ▼                 ┌──┴───┐
  SELECT from Postgres  │      │
      ▼            published   timeout
  SET key, TTL          │      │
      ▼                 ▼      ▼
   release lock      return   SELECT from Postgres  ◄── read through, never block forever
      ▼                       (and SET)
   return
```

Four properties worth calling out:

- **The double-check after winning the lock is what makes this correct
  rather than merely lucky.** Another request may have published between
  our miss and our acquire.
- **The follower fallthrough is the important design choice.** A request
  is never blocked indefinitely waiting for another request's work. A
  pattern that waits unboundedly converts one slow query into worker-pool
  exhaustion and then a total outage — strictly worse than the stampede it
  prevents. The guarantee is deliberately *"far fewer DB hits than
  requests"*, not *"exactly one"*; buying the stronger guarantee costs
  unbounded coupling between unrelated requests.
- **A crashed winner self-heals** via the 5s lock TTL: the next requester
  becomes the winner.
- **Coordination failure is non-fatal.** If Redis errors during lock
  acquisition, `get_or_set` logs it and degrades to plain cache-aside
  (everyone reads through). The lock is a performance optimization, not a
  correctness mechanism — unlike `DistributedLockService.guard`, which
  *does* raise, because there exclusivity is not optional.

Tested by `TestCacheAsideAndStampede::test_stampede_protection_collapses_concurrent_misses`:
50 concurrent requests for one uncached key must produce fewer than 10
source hits, and every caller must still receive a correct answer.

**Not implemented (and why):** probabilistic early expiration
("XFetch" — recompute early with probability rising as TTL approaches)
avoids the synchronized-expiry cliff entirely and composes well with
single-flight. It was left out because it adds a tuning parameter with
non-obvious failure modes, and single-flight already covers the invalidation-
driven case, which is the more common trigger here. A clean future addition.

---

## 9. Distributed locking

The algorithm is Phase 4's, unchanged, because it was already correct:

```
ACQUIRE:  SET lock:<key> <uuid4-token> NX PX <ttl_ms>
          └─ atomic "claim or fail" in one round trip

RELEASE:  EVAL  if redis.call("get", KEYS[1]) == ARGV[1]
                then return redis.call("del", KEYS[1]) else return 0 end
          └─ deletes ONLY if the key still carries OUR token
```

The Lua-guarded release exists to prevent the classic **lost-lock** bug:

```
  A acquires (TTL 10s) ─────────────────────────────────► A's work runs long
  t=10s  A's lock silently expires
  t=11s  B acquires the same key ────────────────────────► B is now the holder
  t=12s  A finishes and calls DEL  ◄── would delete B's lock!
         Lua token check: get(key) != A's token -> returns 0, deletes nothing ✅
```

Phase 7 adds, around that unchanged core:

- `acquire_with_timeout(timeout, interval)` — bounded, **jittered**
  (`uniform(0.5x, 1.5x)`) retry. Jitter matters for the same reason it does
  in `retry_async`: N replicas waiting on one hot key would otherwise poll
  in lockstep. There is deliberately no "wait forever" option.
- `owns()` — authoritative ownership check against Redis (one round trip,
  opt-in), versus `is_held` which is local belief only.
- `release(strict=True)` — raises `LockOwnershipError` instead of
  silently no-op'ing, for call sites where losing the lock mid-section
  means their work may have raced.
- `DistributedLockService` — a facade, **not a second implementation**,
  whose real value is refusing to conflate two failure modes:

| Failure | Meaning | Outcome |
|---|---|---|
| Contention | Someone else holds it | `LockAcquisitionTimeout` → 409 (existing Phase 4 handler) |
| Redis unreachable at **acquire** | Exclusivity cannot be proven | `DistributedLockError` — **never** treated as "the lock is free" |
| Redis unreachable at **release** | Work already happened; TTL will free it | Logged, swallowed. Raising would turn a successful operation into a client-visible error for no benefit. |

**TTL-based expiry, not renewal (Redlock).** A crashed holder blocks others
for at most `ttl_seconds`. There is no heartbeat/renewal thread. For a lock
that must be held longer than its TTL: *raise the TTL*, do not rely on
renewal that does not exist here. Redlock across multiple independent Redis
masters is deliberately out of scope — it is contested in the literature,
and NimbusFS's locks guard performance and convenience (stampede
suppression, duplicate-chunk avoidance), never the *last* line of
correctness. The real guarantees live in Postgres constraints — e.g.
Phase 6's `UniqueConstraint(upload_id, chunk_number)`.

---

## 10. Rate limiting

### Algorithm selection

| Algorithm | Verdict |
|---|---|
| **Fixed window counter** | Cheapest, and wrong at the boundary: full budget in the last instant of one window plus full budget in the first instant of the next = **2x** the intended rate in a sub-second span. On a login endpoint — where the entire point is throttling credential stuffing — the doubling lands exactly where it hurts. |
| **Sliding window log** (sorted set of timestamps) | Exactly accurate, memory linear in request rate. Every request stores a member for a full window; at 300 req/min/user across many users that buys precision nobody can perceive, plus O(log N) trims per call. |
| **Sliding window counter** (weighted two-window blend) | Good middle ground, but it is an *approximation* and cannot express burst separately from sustained rate — exactly the distinction an API wants. |
| **Token bucket** ✅ | Two numbers per key (`tokens`, `ts`): O(1) memory, O(1) time. Burst and sustained rate are separate tunables (`capacity` = max burst, `capacity/window` = refill rate/s). And it yields an **exact** `Retry-After` for free — the time until enough tokens accrue — where the counter approaches can only guess. A client told precisely when to return does not poll. |

```
capacity ┤ ████████████                     ████████
         │ ████████████                 ████████████
 tokens  │ ████████████             ████████████████
         │ ██████                 ██████████████████
       0 ┼─────┬──────────────────┬───────────────────► time
           burst of N requests    refill at N/W per second
           drains the bucket      until capacity is reached
                                  (never beyond — no banking)
```

### Atomicity

Read-modify-write on a shared counter from N pods is a lost-update race:
two replicas can both read "1 token left" and both allow. Redis is
single-threaded and executes a Lua script atomically, so the whole
refill→check→decrement sequence is indivisible. `WATCH`/`MULTI`/`EXEC`
would need optimistic-retry loops — i.e. the most retries exactly when the
limiter is hottest — plus two round trips instead of one.

`now_ms` is passed **into** the script rather than read via
`redis.call("TIME")`: that keeps the script deterministic (a replication
concern in older Redis) and trivially testable. Clock skew between replicas
is bounded by NTP and at worst shifts a bucket's refill by milliseconds.

### Categories and identity

Independent budgets per category (`login`, `register`, `metadata`,
`upload_initiate`, `upload_complete`, `search`, `default`) so exhausting
search cannot starve an in-flight upload. Identity is the JWT `sub` claim
when a valid Bearer token is present (decoded locally — signature-verified,
**no database round trip**), else the client IP resolved by
`TrustedProxyMiddleware`.

Three consequences, stated deliberately:
- An invalid/expired token is limited by **IP** — correct, since an
  attacker brute-forcing tokens has no identity.
- Users behind one NAT share an IP bucket on `login`/`register`. That is
  the intended credential-stuffing defense, and why those categories get
  budgets generous relative to one human's usage.
- **No authorization happens here.** The token is an identity *hint* for
  bucketing only; every route still runs `CurrentUser` and its own
  ownership checks. A forged identity buys an attacker a different bucket,
  never access.

### Why a dependency, not middleware

Middleware sees only a method and a path string, so route classification
means a path-pattern table that rots the moment a route is renamed. A
dependency (`dependencies=[Depends(rate_limit(RateLimitCategory.SEARCH))]`)
lives next to the endpoint, moves with it, appears in the OpenAPI schema,
runs before the handler body (so a rejected request costs no DB/GCS work),
and is overridable per-route in tests. `/folders` and `/metadata` apply
the `METADATA` budget at **router** level so a newly-added route cannot
silently be unprotected; `/metadata/search` stacks the tighter `SEARCH`
budget on top.

Per-**chunk** `PUT /uploads/{id}/chunks/{n}` is deliberately **not**
limited: a single large upload legitimately issues thousands of parallel
chunk PUTs (the entire point of Phase 6), so a per-request budget there
would throttle correct behavior. Session initiate and complete are the
low-cardinality choke points instead.

### Failure policy

`RATE_LIMIT_FAIL_OPEN` (default **true**): Redis unreachable → allow, log
at ERROR with `rate_limit_degraded`. For a file-storage platform whose
users are mid-upload, turning a Redis blip into a fleet-wide 429 storm
converts a degraded dependency into a total outage. Rate limiting here is
abuse mitigation, not an authorization control, and it is not the last line
of defense (GCLB and GCP edge protections sit in front). Set false to fail
closed for a public unauthenticated tier — that path is implemented and
tested, not merely described.

Either way the event is logged loudly. A limiter that has been silently
allowing everything for a week is indistinguishable from no limiter.

### The 429 contract

```
HTTP/1.1 429 Too Many Requests
Retry-After: 6
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 0
X-RateLimit-Category: login

{"success": false, "message": "Rate limit exceeded for 'login': 10 requests per 60s.",
 "data": null, "errors": null, "timestamp": "...", "request_id": "..."}
```

`RateLimitExceeded` is the **only** Phase 7 exception needing a new
handler; every other new exception subclasses an already-registered base
and gets correct HTTP mapping for free via FastAPI's MRO walk (the
technique Phase 6 established). Successful responses carry the same
`X-RateLimit-*` headers via `RateLimitHeadersMiddleware`, and unlimited
routes still report `unlimited` rather than omitting the headers — so the
Phase 4 client contract is honored, not broken, now that limits are real.

---

## 11. Failure scenario analysis

| # | Scenario | Detection | Behavior | User impact |
|---|---|---|---|---|
| 1 | **Redis crashes / is unreachable** | Connection error on first command; `/health`,`/ready` report `redis: false` | Every `CacheService` op catches, logs `cache_error`, returns miss/no-op. Every read falls through to Postgres. Rate limiter fails open. Locks at acquire raise `DistributedLockError`. | None functionally. Higher latency, higher Postgres load. Tested: `test_api_keeps_working_with_redis_completely_down`. |
| 2 | **Redis slow (not down)** | `socket_timeout=2s` turns slow into an error | Same as #1, per-command. The tight timeout is the point: a *slow* cache is worse than an *absent* one, because you pay it before falling back anyway. | Up to +2s on the first affected command, then normal fallback. |
| 3 | **Connection pool exhausted** | `redis-py` raises on checkout | Treated exactly like #1 — logged as `cache_error`, degraded. | As #1. **Sizing:** total connections = `REDIS_MAX_CONNECTIONS` (20) × replicas. HPA max 10 → 200 against Memorystore; keep under the instance limit. |
| 4 | **Cache is stale** | Not detectable at read time — that is the nature of the problem | Bounded three ways: explicit invalidation on every write, per-entity TTL as the hard ceiling, and `CACHE_WRITE_GUARD_SECONDS` to close the invalidate-before-commit window. See §7. | ≤ TTL of wrong data in the worst case; typically zero. |
| 5 | **Stampede lock expires mid-populate** | Winner's `owns()` would return False | The lock's 5s TTL lapses, another request becomes the winner and populates. Both write the same value — a duplicate query, not a correctness problem. Nothing here holds a lock across a mutation. | None. |
| 6 | **Lock owner crashes** | Nothing to detect — the process is gone | TTL expiry frees the lock. No shutdown hook is required or assumed, which is correct: `kill -9`/OOM never runs one. `app/main.py`'s lifespan documents this explicitly. | Others wait ≤ lock TTL. |
| 7 | **Rate limiter unreachable** | Redis error inside `RateLimiter.check` | `RATE_LIMIT_FAIL_OPEN=true` → allow, log `rate_limit_degraded` at ERROR, `degraded=True` on the result. `false` → 429. | Fail-open: none (limits temporarily unenforced). Fail-closed: total 429. Both tested. |
| 8 | **Multiple pods, same key, same instant** | — | *Reads:* single-flight collapses them; the losers poll briefly, then read through. *Writes:* last-writer-wins on a `SET`, which is safe because every writer is writing the same DB-derived value. *Invalidation:* `DEL` is idempotent and order-independent. *Rate limits:* one atomic Lua bucket, so N pods enforce ONE budget — tested by `test_concurrent_requests_share_one_bucket`. | None. |
| 9 | **Rolling deploy, two schema versions live** | `v` mismatch in the envelope | Old-version entries read as misses by the new build (and vice versa); both repopulate in their own format. | One cold period. Never a crash — this is exactly what the version field buys. |
| 10 | **Oversized value** (a huge search page) | `len(encoded) > CACHE_MAX_VALUE_BYTES` | Logged `cache_skipped_too_large`, write skipped, request succeeds from Postgres. Search additionally refuses pages over `CACHE_SEARCH_MAX_ITEMS`. | None. Prevents one huge value evicting thousands of hot small ones. |
| 11 | **Redis memory pressure / eviction** | Keys vanish early | Indistinguishable from a TTL lapse — every read path already handles a miss. **Ops note:** configure Memorystore with `maxmemory-policy allkeys-lru`; `noeviction` would turn a full cache into write errors, i.e. scenario #1 with extra steps. | None. |
| 12 | **Poisoned / corrupt cache entry** | JSON or envelope check fails | Treated as a miss, logged with `reason="stale_schema"`. | None. |

---

## 12. Production architecture on GCP (Memorystore)

```
   ┌──────────────────────── VPC (private) ─────────────────────────┐
   │                                                                 │
   │   ┌──────────────── GKE cluster (Phase 5) ─────────────────┐    │
   │   │  Namespace: nimbusfs                                    │    │
   │   │  Deployment 3..10 replicas (HPA on CPU+memory)          │    │
   │   │    each pod: REDIS_MAX_CONNECTIONS=20                   │    │
   │   │    envFrom: nimbusfs-config (CACHE_*, RATE_LIMIT_*)     │    │
   │   │  NetworkPolicy: default-deny + explicit egress allows   │    │
   │   └───────────────┬────────────────────────┬───────────────┘    │
   │                   │ 6379                   │ 5432                │
   │   ┌───────────────▼──────────────┐  ┌──────▼──────────────────┐ │
   │   │ Cloud Memorystore for Redis  │  │  Cloud SQL (PostgreSQL) │ │
   │   │  • Standard tier (HA, replica│  │  • Private IP           │ │
   │   │    + automatic failover)     │  │  • AUTHORITATIVE        │ │
   │   │  • Private Service Access IP │  └─────────────────────────┘ │
   │   │  • maxmemory-policy:         │                              │
   │   │      allkeys-lru             │                              │
   │   │  • AUTH + in-transit TLS     │                              │
   │   └──────────────────────────────┘                              │
   └─────────────────────────────────────────────────────────────────┘
                     │ Private Google Access
                     ▼
              Google Cloud Storage (file bytes — never Redis)
```

**Local dev vs production**

| | Local (`docker-compose.yml`) | Production (Memorystore) |
|---|---|---|
| Instance | `redis:7-alpine` container, no auth | Managed, Standard tier (HA + failover) |
| Address | `REDIS_HOST=redis` (compose network) | Private Service Access IP, e.g. `10.0.0.4` |
| Auth/TLS | none | AUTH string in the Secret; in-transit encryption on |
| Persistence | none needed | none needed — **Redis holds nothing that matters** |
| Eviction | default | `allkeys-lru` (see failure #11) |
| Failure practice | `docker compose stop redis` and watch the API keep serving | The same behavior, exercised by a real failover |

**Kubernetes/GKE compatibility.** Nothing in Phase 7 changes the
statelessness Phase 4 established or the manifests Phase 5 wrote:

- No pod-local cache, no sticky sessions. All cache state is in
  Memorystore, shared identically by every replica, so a request may land
  on any pod. Scaling 3→10 or evicting a pod changes nothing.
- Rate limit buckets are shared, so the *effective* limit is per-user, not
  per-user-per-pod. An in-process limiter would have silently multiplied
  every budget by the replica count.
- Locks are TTL-based, so a `SIGKILL`ed pod self-heals with no shutdown
  hook — matching the PodDisruptionBudget/rolling-update model.
- The only manifest change this phase is additive keys in
  `k8s/05-configmap.yaml`, consumed by the existing `envFrom` — no
  `07-deployment.yaml` change. `11-networkpolicy.yaml` already allows
  egress to the Memorystore CIDR (still a placeholder that must be
  replaced with the real range before a real deploy — unchanged Phase 5
  caveat).

---

## 13. Observability

Structured `structlog` events only — a full Prometheus/OpenTelemetry stack
is explicitly out of scope this phase. The events are written to be
*trivially* scrapable into metrics later: stable event names, flat
key/value pairs, and a numeric `duration_ms` on everything.

| Event | Level | Key fields |
|---|---|---|
| `cache_hit` / `cache_miss` | debug | `operation`, `cache_key`, `duration_ms`, `reason` (`absent` vs `stale_schema`), `result` |
| `cache_set` | debug | `cache_key`, `ttl_seconds`, `size_bytes`, `duration_ms` |
| `cache_delete` / `cache_invalidated` / `cache_invalidation` | debug/info | `key_count`, `removed`, `mode`, `operation` |
| `cache_error` | **error** | `operation`, `cache_key`, `cache_key_hash`, `error_type`, `error`, `duration_ms`, `result="degraded_to_source"` |
| `cache_skipped_too_large` | warning | `size_bytes`, `max_value_bytes` |
| `cache_stampede_leader` / `_follower_served` / `_follower_read_through` | debug/info | `cache_key`, `polls`, `waited_seconds` |
| `lock_acquired` / `lock_contended` / `lock_acquire_timeout` | debug/info/warning | `lock_key`, `attempts`, `duration_ms` |
| `lock_released` / `lock_release_not_owned` / `lock_redis_error` | debug/warning/error | `lock_key`, `phase` |
| `rate_limit_allowed` / `rate_limit_rejected` | debug/warning | `category`, `identity_type`, `identity_hash`, `limit`, `remaining`, `retry_after_seconds` |
| `rate_limit_degraded` | **error** | `category`, `fail_open`, `error_type`, `result` |

`request_id` / `correlation_id` / `trace_id` / `server_id` are **not**
passed explicitly anywhere: `RequestContextMiddleware` binds them into
structlog's contextvars for the whole request, so they land on every line
above automatically — including across `await` boundaries.

**Rate-limit identities are hashed, never logged raw.** They are either a
user ID or a client IP, both personal data with no business in a log
aggregator. The hash is stable, so "this same caller keeps getting
limited" is still answerable. `CacheKeyBuilder.redact` does the same job
for any key that might carry user-derived text.

**Silence and fallback are different things.** Every degradation path logs
before it degrades. A cache that has been failing for a week while the app
quietly serves from Postgres at higher latency is a far worse incident
than a loud one.

---

## 14. Interview questions

### Beginner

**Q: What is caching, and why put Redis in front of PostgreSQL when Postgres already has its own buffer cache?**
Caching stores the result of an expensive operation so a repeat request can skip the work. Postgres's shared buffers do cache *pages*, but a request still pays: connection checkout, query planning, execution, row→ORM object hydration, and Pydantic serialization. A Redis hit skips all of it and returns a ready-to-emit JSON payload. Redis also scales independently of the database and absorbs read load that would otherwise consume connections from a pool that is the real horizontal-scaling ceiling (see `app/database/session.py`).

**Q: What is cache-aside?**
On read: check the cache; on a miss, read the database, write the result to the cache, return it. On write: update the database and **delete** the cache entry. The cache is opportunistic — empty, partial, or entirely absent, the system is still correct.

**Q: Why does every cached entry have a TTL?**
It is the hard ceiling on staleness if invalidation is ever missed, dropped, or raced. It also reclaims memory from entries nobody reads any more. Without a TTL, one missed invalidation is stale forever.

**Q: What happens to NimbusFS if Redis dies?**
Nothing user-visible. Every cache operation catches the failure, logs it, and returns "as if the cache did not exist"; reads fall through to Postgres, which is authoritative. Rate limiting fails open by default. There is a test that literally kills the fake Redis mid-suite and asserts the API keeps answering.

**Q: Why is file content never stored in Redis?**
Redis is in-memory, non-durable, and shared. Storing bytes there would be expensive, would evict everything useful, and would create a second source of truth for content that GCS already owns durably. `CacheSerializer.encode` raises on `bytes` so it cannot happen by accident.

### Intermediate

**Q: Why JSON and not pickle?**
Three independent reasons, any one sufficient: `pickle.loads` on a shared, network-reachable, multi-writer datastore is arbitrary code execution; pickle encodes class paths so a routine rename breaks every entry mid-rolling-deploy; and pickle is unreadable from `redis-cli` during an incident. Full detail in §4.

**Q: How do you cache without breaking authorization?**
Per entity, and it is decided deliberately rather than by default. Folder/file/user entries are keyed **by resource**, and the cached payload carries `owner_id`; the service re-applies exactly the ownership + not-deleted filter the repository's WHERE clause would have applied, raising the same 404 (never a 403 — IDs must stay unguessable). Search is different: a result set has no single owner to check, so its key is **caller-scoped** (`search:{owner_id}:{fp}`) and can only ever be read by that user. Nothing anywhere caches an authorization *decision*. There is a test where user B tries to read a folder user A just warmed into the cache, and gets a 404.

**Q: A folder rename must invalidate what, exactly?**
The folder's own entry, its children listings (all sort/filter variants), its breadcrumbs, and the parent's children listings. On a *move*, both the old and new parent. Descendant breadcrumbs are deliberately left to TTL — see §7 race #2 for the reasoning and the bounded, cosmetic consequence.

**Q: Why delete on write instead of updating the cache?**
Delete is idempotent and order-independent; the loser of a race just causes one extra read. Update is neither: two concurrent writers can apply cache updates in the opposite order to their DB commits, leaving the cache permanently disagreeing with Postgres with no TTL-independent way to detect it.

**Q: Why is `KEYS` never used for pattern invalidation?**
`KEYS` is O(N) over the entire keyspace and blocks Redis's single command thread for the duration. On a production instance with millions of keys that is a self-inflicted outage. `SCAN` is cursor-based and incremental; `CacheService.scan_keys` also bounds the worst case.

**Q: Why token bucket over sliding window?**
Sliding-window *log* is exact but memory-linear in request rate. Sliding-window *counter* is an approximation that cannot separate burst from sustained rate. Fixed window allows 2x the intended rate across a boundary. Token bucket is O(1) in both memory and time, expresses burst (`capacity`) and sustained rate (`capacity/window`) as separate tunables, and yields an exact `Retry-After` from the token deficit.

**Q: Why must the rate-limit check be a single Lua script?**
Because read-modify-write from N replicas is a lost-update race — two pods can both read "1 token left" and both allow. Redis executes a Lua script atomically on its single command thread, making refill→check→decrement indivisible. `WATCH`/`MULTI`/`EXEC` would need optimistic retries, which peak exactly when the limiter is hottest.

### Advanced

**Q: Walk through a cache stampede and your mitigation, including what it does NOT guarantee.**
A hot key expires under load and every concurrent request misses simultaneously, converting one query into hundreds against an already-busy database. Mitigation is single-flight: on a miss, requests race for a short-TTL Redis lock; the winner re-checks the cache (this double-check is what makes it correct rather than lucky), loads from Postgres, publishes, and releases. Followers poll the cache briefly and then — critically — **read through to Postgres anyway** rather than waiting indefinitely. So the guarantee is "far fewer DB hits than requests", **not** "exactly one". Buying the stronger guarantee would mean unbounded blocking, which converts one slow query into worker-pool exhaustion and then a total outage — strictly worse than the stampede. The lock's TTL additionally bounds a crashed winner. Probabilistic early expiration (XFetch) would complement this and is a noted future addition.

**Q: There is a race between your invalidation and your transaction commit. Describe it and why you shipped anyway.**
Invalidation is issued inside the request's transaction, which commits at the request boundary. Between the writer's `DEL` and its `COMMIT`, a concurrent reader can miss, read the still-old committed row, and write it back — leaving a stale entry that outlives the write by a full TTL. It shipped because it is bounded three ways: the window is the remainder of one transaction (sub-millisecond in practice), staleness is capped at the entity TTL rather than unbounded, and `CACHE_WRITE_GUARD_SECONDS` closes it entirely via a post-invalidation tombstone that rejects the stale write (implemented and tested, off by default because it leaves every written key cold for its duration). The airtight fix — invalidating in a SQLAlchemy `after_commit` hook — needs a transaction-lifecycle hook the current per-request Unit of Work does not expose to services. That is documented as a real limitation, not hidden.

**Q: Your distributed lock releases with a Lua script. Why can't it just be `DEL`?**
Because of the lost-lock bug. If holder A's work outlives its TTL, the lock expires, B acquires it, and A's later `DEL` would delete *B's* lock — silently allowing two holders. The script deletes only if the key still carries A's own random token. The check and the delete must be atomic, which is why it is a script and not a `GET` followed by a `DEL`.

**Q: Is your lock safe under a Redis failover? Would Redlock fix it?**
No, and not really. With a single Redis master, an asynchronously-replicated failover can lose a just-written lock key, letting two holders exist briefly. Redlock (quorum across independent masters) attempts to address this and is genuinely contested — Kleppmann's critique versus antirez's response — because it still leans on bounded clock drift and GC pauses. NimbusFS deliberately does not go there, because its locks never carry the *final* correctness guarantee: they suppress stampedes and make duplicate work rare. The real guarantees are Postgres constraints — Phase 6's `UniqueConstraint(upload_id, chunk_number)` is what actually prevents duplicate chunks; the lock only makes the race rare. That layering is the point: never let a lock be the only thing standing between you and corruption.

**Q: Why fail *open* on rate limiting when a security control failing open is normally wrong?**
Because this is not a security control in the authorization sense. It is abuse mitigation, it sits behind GCLB and GCP's own edge protections, and its failure mode matters: fail-closed turns a Redis blip into a fleet-wide 429 storm — a degraded dependency becoming a total outage — for a platform whose users are mid-multi-gigabyte-upload. So the default is fail-open with a loud ERROR log and a `degraded=True` flag on the result. It is a config flag, not a hardcoded belief: `RATE_LIMIT_FAIL_OPEN=false` is implemented and tested for deployments (a public unauthenticated tier) where the trade-off inverts.

**Q: You cache a `Page` of search results including `total`. What could go wrong, and how is it bounded?**
`total` comes from a separate COUNT, so a cached page can report a count that no longer matches reality — and unlike a single row, its membership can be invalidated by *any* file write, with no way to know which cached queries matched without re-running them. Bounded three ways: the shortest TTL in the system (90s), a coarse per-user `SCAN`-and-delete of `search:{owner}:*` on every file mutation, and a hard refusal to cache pages over `CACHE_SEARCH_MAX_ITEMS` so deep pagination cannot evict the hot working set. Precise fan-out would need a reverse index from row to query, which is a search-engine feature rather than a cache feature.

**Q: You added a `v` field to every cached value. What incident does that prevent?**
A rolling deploy where the payload shape changed. Both builds read the same Redis simultaneously; without a version, the new build deserializes an old-shaped payload into a Pydantic model, raises a validation error deep inside a read path, and 500s — on a fraction of requests, non-deterministically, for as long as old entries survive. With it, a mismatched version is treated as a cache **miss**: the request falls through to Postgres and repopulates. The cost of a format change becomes one cold period instead of a partial outage. The same code path also absorbs corrupt entries and anything else that fails to decode.

**Q: How would you extend this to per-user file *sharing* without introducing an authorization bug?**
The current design is safe precisely because a file has exactly one owner, so a resource-scoped key with an owner re-check is sound. Sharing breaks that: the same resource would have different visibility per caller. Two viable answers. (1) Keep the resource-scoped entity cache — the file's *representation* is still identical for everyone allowed to see it — and cache the *permission set* separately under `nimbusfs:file:{id}:acl`, re-checking it on every read; invalidate the ACL key on any grant/revoke. (2) Fall back to caller-scoped keys (`file:{id}:viewer:{user_id}`), which is simpler to reason about but multiplies cache entries by the sharing fan-out and makes invalidation on revoke a SCAN. I would take (1), because the entity cache stays shared (which is where the hit rate lives) and only the small, cheap ACL is duplicated. The docstrings in `metadata_service.py` and `folder_service.py` explicitly flag themselves as the places that must be revisited when this lands.

**Q: What is still missing from this phase, honestly?**
Four things. (1) Invalidation is not post-commit — §7 race #1 — though `CACHE_WRITE_GUARD_SECONDS` now defaults ON (1.5s) and closes it in the common case; the airtight fix still needs an `after_commit` hook the Unit of Work doesn't expose. (2) No negative caching, so a hot 404 hits Postgres every time (deliberate: it would make a just-created resource 404 for a full TTL). (3) No probabilistic early expiration, so synchronized TTL expiry of a very hot key still causes one brief single-flight event. (4) No cache warming and no metrics backend — hit rates are visible only as structured logs, and after every deploy the cache is cold. Descendant-breadcrumb staleness on ancestor rename, previously a fifth item here, is fixed — see §7 Race #2. All remaining items are noted as clean future additions, none is load-bearing for correctness.

---

## 15. Completion checklist

- [x] Redis pool reused (not duplicated), with config-driven timeouts, retry, health-check interval, graceful shutdown
- [x] `CacheKeyBuilder` — centralized, collision-safe, predictable, with SCAN patterns for invalidation
- [x] `CacheSerializer` — JSON (never pickle), datetime/UUID/Decimal/Enum/set/BaseModel support, versioned envelope, unknown version = miss
- [x] `CacheService` — get/set/delete/exists/expire/increment/get_or_set/invalidate/scan, every Redis failure logged and degraded
- [x] `CachePolicy` — per-entity TTLs, all from `Settings`
- [x] `CacheInvalidator` — operation-named fan-out, delete-never-update, documented simplifications
- [x] Cache-aside with single-flight stampede protection and a bounded follower wait
- [x] `DistributedLockService` — bounded acquire, ownership validation, strict release, contention vs infrastructure-failure separation
- [x] `RateLimiter` — atomic Lua token bucket, per-category budgets, configurable fail-open/closed
- [x] New exceptions, all subclassing registered bases except the one 429 handler that genuinely needs `Retry-After`
- [x] Service integration: user, folder (metadata/children/breadcrumbs), file metadata, versions, search
- [x] Rate limits wired to auth, folder/metadata APIs, upload initiate/complete, search — as dependencies
- [x] Authorization preserved on every cached path, analyzed and documented per entity type
- [x] All settings in `Settings` + `.env.example` + `k8s/05-configmap.yaml`
- [x] Structured logs for every cache/lock/rate-limit event, metrics-ready, identities hashed
- [x] Tests: 101 new (`tests/test_caching.py`, `tests/test_rate_limiting.py`), 246/246 total passing, zero regressions
- [x] `FakeRedisClient` extended (hashes, SCAN, INCR, EXPIRE, TTL, token bucket, controllable clock, failure injection)
- [x] Benchmark script + runbook, with no fabricated numbers
- [x] `README.md` §14, this design doc, `CONTEXT.md` updated
