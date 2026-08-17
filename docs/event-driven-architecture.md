# NimbusFS — Event-Driven Architecture (Phase 8)

The standalone technical deep-dive for Phase 8: Google Cloud Pub/Sub plus
a transactional outbox. README §15 is the walkthrough; this document is
the part that does not fit there — the hazard analysis, the timing
arguments, the classification table, the operational runbook, and the
failure catalogue.

Companion to `docs/PHASE_7_REDIS_DESIGN.md`, same shape and same bluntness
about gaps.

---

## 1. The dual-write hazard, fully

### 1.1 What "dual write" means

An operation that must change two systems, with no transaction spanning
both. Here: `INSERT INTO file_metadata` in Postgres, and "publish
`file.uploaded`" to Pub/Sub. There is no distributed transaction between
a relational database and a managed message broker — and even if there
were (XA/two-phase commit), you would not want one: 2PC turns two
independent availability domains into one, so the *combined* system is
less available than either part.

### 1.2 The four orderings, and why three are broken

**(a) Commit, then publish.**

```
COMMIT ──────► [crash] ──────► publish
```

The file exists. No event exists. No thumbnail, no notification, ever —
and, critically, **nothing in the system knows anything is missing**.
This is the worst outcome because it is *silent*: there is no error, no
retry counter, no queue depth to alarm on. You find out when a user asks
why their photo has no preview, months later.

**(b) Publish, then commit.**

```
publish ──────► [crash/rollback] ──────► COMMIT (never happens)
```

An event exists for a file that does not. Every consumer fails on it —
the file worker's GCS check finds nothing, the thumbnail worker finds no
metadata row. Those are classified non-retryable, so each burns a
`ProcessedEvent(FAILED)` row and gets acked. Not silent, but it is a
manufactured lie about the state of the world, and any consumer written
slightly less defensively would act on it.

**(c) Publish inside the transaction.**

Does not help. The publish is not transactional; it happens immediately
and irreversibly, and the surrounding `ROLLBACK` cannot recall it. This
is (b) with extra steps and a false sense of safety.

**(d) Outbox — the one that works.**

```
BEGIN
  INSERT file_metadata ...
  INSERT outbox_events (status=PENDING) ...
COMMIT                      ← one atomic outcome, one system

  ... later, a separate process ...
  SELECT ... FOR UPDATE SKIP LOCKED
  publish
  UPDATE status=PUBLISHED
  COMMIT
```

Both the business fact and the *intent to publish* live in one
transaction in one system. They cannot disagree. Delivery becomes a
separate, retryable problem — which is a solved problem, unlike
"we lost the fact that this happened."

### 1.3 What the outbox does not give you

Not exactly-once. The publisher's own two steps (publish to Pub/Sub;
mark the row PUBLISHED) are themselves a dual write, and the hazard is
merely *moved*, not eliminated. It is moved somewhere much better,
though: the residual window is now

```
publish succeeded ──► [crash] ──► mark PUBLISHED (never runs)
```

which leaves the row `PENDING`, republished on the next poll. A
**duplicate**, not a loss. That asymmetry is the entire design. The
inverse ordering (mark published first, then publish) would trade a
harmless duplicate for a silent loss — strictly worse, and the mistake is
easy to make because "mark it done then do it" reads more optimistic.

Duplicates are then absorbed by consumer-side idempotency (§3). The chain
is: **atomic intent → at-least-once delivery → idempotent consumption =
effectively-once processing.** Each link is boring on its own; the
combination is the guarantee.

### 1.4 Alternatives, honestly assessed

| Approach | Why not here |
|---|---|
| 2PC / XA | Couples availability domains; poor support in both Postgres drivers and Pub/Sub; operationally miserable. |
| CDC (Debezium / logical decoding) | Genuinely better at high volume — no polling, no publisher process. Costs: a connector to operate, a replication slot that fills your disk when a consumer stalls, and schema-evolution coupling between your tables and your event contract. Wrong size of solution for one service. |
| Publish from a DB trigger | Puts network I/O inside a transaction, holding locks for the duration of an RPC. |
| Just retry the publish in-process | Does not survive process death, which is the failure being defended against. |
| Accept the loss | Defensible for genuinely low-value events. Not defensible when you cannot tell which events were lost. |

The polled outbox is the right answer *at this scale*. If NimbusFS ever
publishes tens of thousands of events per second, CDC becomes correct and
the migration path is clean — the `outbox_events` table is exactly what a
Debezium connector would tail.

---

## 2. Sequence diagrams

### 2.1 Happy path: upload → thumbnail + notification

```
Client   API            Postgres        outbox-pub   Pub/Sub    file-wkr   thumb-wkr  notify-wkr
  │       │                 │                │          │          │          │          │
  │ POST  │                 │                │          │          │          │          │
  ├──────►│                 │                │          │          │          │          │
  │       │ BEGIN           │                │          │          │          │          │
  │       ├────────────────►│                │          │          │          │          │
  │       │ INSERT file_metadata             │          │          │          │          │
  │       │ INSERT file_versions             │          │          │          │          │
  │       │ INSERT outbox_events (PENDING)   │          │          │          │          │
  │       │ COMMIT  ◄── ONE atomic outcome   │          │          │          │          │
  │ 201   │                 │                │          │          │          │          │
  │◄──────┤   (user is done here; nothing below is on the request path)       │          │
  │       │                 │                │          │          │          │          │
  │       │                 │◄── poll ───────┤          │          │          │          │
  │       │                 │  SELECT ... FOR UPDATE SKIP LOCKED   │          │          │
  │       │                 │                ├─publish─►│          │          │          │
  │       │                 │◄─ UPDATE PUBLISHED ───────┤          │          │          │
  │       │                 │   COMMIT (per row)        │          │          │          │
  │       │                 │                │          ├─deliver─►│          │          │
  │       │                 │                │          │  HEAD object (GCS)  │          │
  │       │                 │◄── has_processed? ─────────────────── ┤          │          │
  │       │                 │                │          │◄─ publish thumbnail.requested ─┤
  │       │                 │                │          │◄─ publish notification.req ────┤
  │       │                 │◄── INSERT processed_events + COMMIT ──┤          │          │
  │       │                 │                │          │◄─ ACK ───┤          │          │
  │       │                 │                │          ├──── deliver ───────►│          │
  │       │                 │                │          │       download+decode+upload   │
  │       │                 │◄── UPDATE thumbnail_object_name ──────────────── ┤          │
  │       │                 │◄── INSERT processed_events + COMMIT ─────────────┤          │
  │       │                 │                │          │◄──── ACK ───────────┤          │
  │       │                 │                │          ├───────── deliver ─────────────►│
  │       │                 │◄── INSERT notifications + processed_events + COMMIT ────────┤
  │       │                 │                │          │◄───────── ACK ─────────────────┤
```

The load-bearing detail: in every consumer, the **work and its
`processed_events` row commit together**. Split them and a crash in
between re-runs the work on redelivery — re-notifying a user, or (in a
future consumer with side effects) worse.

### 2.2 Pub/Sub unavailable

```
API ──► Postgres: COMMIT (file + outbox PENDING)     ← user unaffected, 201 returned
outbox-pub ──► Pub/Sub: publish ──► ✗ UNAVAILABLE
outbox-pub ──► Postgres: mark_failed
                          attempt_count = 1
                          status = FAILED            ← "retry later", NOT "give up"
                          next_attempt_at = now + 2s
                          last_error = "..."
                          published_at = NULL        ← explicitly pinned; see §7.2
   ... poll ... row not due ... poll ... row not due ...
   next attempt: +2s, then +4s, +8s ... capped at 300s
Pub/Sub recovers ──► next due poll publishes ──► PUBLISHED
```

Uploads keep working throughout. Backoff matters because the dominant
failure mode here hits **every row at once** — without it the publisher
spins at `OUTBOX_POLL_INTERVAL` against a service already struggling,
turning someone else's outage into a self-inflicted retry storm.

### 2.3 Consumer crash mid-message

```
Pub/Sub ──► worker: deliver (ack deadline starts, PUBSUB_ACK_DEADLINE=60s)
worker: process() ... [POD KILLED]
        no ack, no nack
Pub/Sub: ack deadline expires ──► redeliver (delivery_attempt=2)
worker(new pod): has_processed(event_id, consumer)?
        ├── no  ──► process() again. Safe: process() is idempotent by contract.
        └── yes ──► skip, ACK. (The previous pod committed work+ledger together
                     and died before acking — the ledger is the proof.)
```

Note the second branch is *only* correct because work and ledger are one
transaction. If the worker had committed its work and then separately
recorded the ledger row, a crash between them would land in the first
branch with the work already done.

---

## 3. Idempotency

### 3.1 The mechanism

```sql
CREATE TABLE processed_events (
    id           UUID PRIMARY KEY,
    event_id     UUID NOT NULL,
    consumer_name VARCHAR NOT NULL,
    status       processed_event_status NOT NULL,   -- SUCCEEDED | FAILED
    error        TEXT,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_processed_events_event_consumer UNIQUE (event_id, consumer_name)
);
```

**The unique constraint is the guarantee. The pre-check is an
optimization.** This distinction is not pedantry — it is the difference
between a design that works under concurrency and one that works in a
demo. Two replicas can both pass `has_processed()` at the same instant;
only one can win the `INSERT`. The loser catches `IntegrityError`, logs
`duplicate_event_absorbed`, and **still ACKs**, because the winner's
identical work already succeeded and a NACK would request work that is
definitionally done.

`ProcessedEventRepository.record()` attempts the insert inside a
**SAVEPOINT**, so a losing race does not abort the whole transaction and
take the consumer's actual work down with it — the same technique Phase 6
used for `UploadChunkRepository.create_or_get_existing`.

### 3.2 Why the key is `(event_id, consumer_name)`

Three consumers legitimately process overlapping events. A ledger keyed
on `event_id` alone would mean whichever worker recorded first blocked
the other two — silently, and only under the specific interleaving where
they overlap. Keying per consumer makes each consumer's ledger its own.

`consumer_name` is also deliberately kept **separate from `worker_name`**
in `BaseWorker`. Renaming a Deployment must never reset a consumer's
idempotency ledger, because every historical event would then look
unprocessed and a replay would re-run all of it.

### 3.3 Deterministic derived event IDs

The subtlest bug in the whole phase, and the one with no visible symptom:

```python
# tests/test_file_processing_worker.py exists largely to catch this.
def derive_event_id(parent_event_id, child_event_type):
    return uuid.uuid5(DERIVED_EVENT_NAMESPACE, f"{parent_event_id}:{child_event_type.value}")
```

When the file worker's fan-out is retried, it must publish the **same**
child `event_id`s it published the first time. With `uuid4()` every retry
mints fresh identities, downstream `ProcessedEvent` deduplication never
fires, and the thumbnail worker regenerates a thumbnail on every
redelivery forever. Nothing errors. Nothing alarms. It just costs money
and CPU, indefinitely.

UUIDv5 over a fixed namespace makes the derivation a pure function of the
parent event — stable across processes, restarts and deployments.

### 3.4 The limit of idempotency

`ProcessedEvent` prevents *repeated* work in the common case. It does not
make `process()` free to be non-idempotent: a crash after the work but
before the commit genuinely re-runs it. That is why every `process()`
implementation is written to be safe to re-run — deterministic thumbnail
object names that overwrite rather than accumulate, an append-only
notification row guarded by the ledger, a validation step with no side
effects at all.

---

## 4. Ack timing

### 4.1 The three options

| Strategy | Consequence |
|---|---|
| ACK on receipt | A crash mid-processing loses the message permanently. Fast, and wrong for anything that matters. |
| **ACK after processing** (chosen) | A crash costs a redelivery. Requires idempotent consumers, which we have. |
| ACK after processing *and* a downstream confirmation | Only meaningful with an end-to-end confirmation to wait for. There is none here. |

### 4.2 The full decision table

Implemented once, in `BaseWorker._handle`. If each worker made this
decision for itself they would drift, and an inconsistent ack policy is
how events get silently dropped.

| Outcome | `ProcessedEvent` written | Settle | Reasoning |
|---|---|---|---|
| Pre-check hit | (already exists) | ACK | Already done by this consumer. |
| `process()` returns | SUCCEEDED | ACK | Normal. |
| `NonRetryableEventError` | FAILED | ACK | Permanent; redelivery cannot help. Queryable record instead of DLQ noise. |
| Envelope unparseable | none* | ACK | No `event_id` to key a ledger row on. Same bytes redeliver to the same failure. |
| `interested_in()` false | none | ACK | Declined, not processed. Recording it would pollute the ledger that answers "did this consumer handle this event?". |
| Any other exception | none | NACK | Retryable by default — "try again" is the safe answer when you do not know what broke. |
| `IntegrityError` on the ledger | (winner's) | ACK | Lost the race; the work is done. |
| Ledger write fails otherwise | none | NACK | Redelivery is safe; losing the ledger entry silently is not. |

\* Logged at ERROR with the raw failure. This is the one path where a
message disappears without a durable record, and it is accepted because
the alternative — NACKing forever — is an infinite loop on bytes that
will never improve.

Every path ends in exactly one `ack()` or one `nack()`. A message that is
neither stalls until the ack deadline expires, which is the worst of both
outcomes: the latency of a NACK with the invisibility of an ACK.

### 4.3 Ack deadline vs. processing time

`PUBSUB_ACK_DEADLINE` (60s) must exceed the p99 processing time of the
slowest consumer, which is the thumbnail worker. If it does not, Pub/Sub
redelivers work that is still in flight — two pods decoding the same
image, the ledger absorbing one of them after both paid the cost. The
client library extends the deadline automatically while a callback runs,
but the configured value is what applies if the process is wedged rather
than merely slow.

**If thumbnail p99 ever approaches 60s, raise the deadline before raising
replica count.** More replicas against a too-short deadline multiplies
duplicate work rather than reducing latency.

---

## 5. Retryable vs. non-retryable

Getting this wrong in either direction is expensive. Treating a transient
error as permanent **drops real work** with an ACK. Treating a permanent
error as transient burns every delivery attempt and fills the DLQ with
messages no human can act on — and a noisy DLQ is one nobody reads.

| Condition | Class | Why |
|---|---|---|
| GCS timeout / 5xx / auth blip | Retryable | Infrastructure. Will likely succeed on retry. |
| GCS object does not exist | **Non-retryable** | The bytes are gone. Redelivery cannot restore them. |
| Postgres unreachable | Retryable | Infrastructure. |
| Pub/Sub publish failure (fan-out) | Retryable | Infrastructure; the whole message is re-run. |
| Envelope fails to parse | **Non-retryable** | Same bytes forever. |
| Payload missing required fields | **Non-retryable** | The producer is this same codebase — a missing field is a bug, not a blip. |
| Unsupported content type | **Non-retryable** | Will never be supported by *this* build. |
| Corrupt / truncated image bytes | **Non-retryable** | The bytes are what they are. |
| Content type lying about format | **Non-retryable** | Rejected by pinning the decoder; a permanent property of the object. |
| `FileMetadata` row gone | **Non-retryable** | Permanently deleted between upload and thumbnailing. A real race with a permanent answer. |
| Malformed `file_id` in payload | **Non-retryable** | Producer bug. |
| Unknown `notification_type` | *Neither* — falls back | A newer producer introducing a type before consumers know it is the additive change the versioning contract permits. A generic template is a better answer than crash-looping or dropping. |
| Anything unclassified | Retryable | The safe default. |

The rule of thumb: **is this failure a property of the message, or of the
world right now?** Message properties are permanent. World properties are
transient.

---

## 6. Dead-letter queue

### 6.1 What the DLQ is and is not for

The DLQ receives messages that exhausted `MAX_DELIVERY_ATTEMPTS`
retryable attempts — i.e. things that kept failing transiently. It is a
**human triage queue**: someone looks, fixes the underlying cause, and
replays.

Non-retryable failures never go there. A permanently unsupported file
type will not succeed on attempt 5 any more than attempt 1, and routing
it to the DLQ turns a queue that should contain actionable items into
one that contains mostly noise — at which point the team stops reading
it, and the actual incident arrives unnoticed. Those failures get a
`ProcessedEvent(status=FAILED, error=...)` row instead, which is
queryable, joinable and durable.

### 6.2 Provisioning (not automated in Phase 8)

```bash
# One dead-letter topic per subscription's failure domain.
gcloud pubsub topics create nimbusfs-file-events-dlq
gcloud pubsub subscriptions create nimbusfs-file-events-dlq-sub \
    --topic=nimbusfs-file-events-dlq

gcloud pubsub subscriptions update nimbusfs-file-events-file-worker-sub \
    --dead-letter-topic=nimbusfs-file-events-dlq \
    --max-delivery-attempts=5

# Pub/Sub itself needs permission to publish to the DLQ and to ack the
# source subscription — a step that is easy to miss, and whose symptom is
# "dead-lettering silently does not happen".
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
SERVICE_ACCOUNT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
gcloud pubsub topics add-iam-policy-binding nimbusfs-file-events-dlq \
    --member="serviceAccount:${SERVICE_ACCOUNT}" --role=roles/pubsub.publisher
gcloud pubsub subscriptions add-iam-policy-binding nimbusfs-file-events-file-worker-sub \
    --member="serviceAccount:${SERVICE_ACCOUNT}" --role=roles/pubsub.subscriber
```

### 6.3 Runbook: a DLQ has messages in it

**1. Establish scope before touching anything.**

```bash
gcloud pubsub subscriptions describe nimbusfs-file-events-dlq-sub \
    --format='value(name)'
# Backlog size:
gcloud monitoring time-series list \
  --filter='metric.type="pubsub.googleapis.com/subscription/num_undelivered_messages"'
```

**2. Read a sample without consuming it.** `--auto-ack` on a DLQ during
triage destroys the evidence you are triaging.

```bash
gcloud pubsub subscriptions pull nimbusfs-file-events-dlq-sub --limit=10
```

**3. Cross-reference the ledger.** The envelope's `event_id` is the join
key across every table:

```sql
-- What did each consumer do with this event?
SELECT consumer_name, status, error, processed_at
FROM processed_events WHERE event_id = '<event_id>';

-- Where did it come from?
SELECT event_type, aggregate_type, aggregate_id, status,
       attempt_count, last_error, created_at, published_at
FROM outbox_events WHERE event_id = '<event_id>';

-- Everything in the same user operation:
SELECT event_id, event_type, created_at
FROM outbox_events WHERE correlation_id = '<correlation_id>' ORDER BY created_at;
```

A DLQ message with **no** `processed_events` row means it never completed
processing — consistent with a retryable failure loop, which is what the
DLQ is for. A DLQ message *with* a SUCCEEDED row means an ack was lost
after the work committed; the replay will be absorbed as a duplicate and
is harmless.

**4. Classify.**

| Pattern | Cause | Action |
|---|---|---|
| All from one time window | A transient dependency outage | Fix/wait, then replay all |
| All the same `event_type` | A consumer bug for that type | Fix the code, deploy, replay |
| All the same `aggregate_id` | Bad data for one entity | Fix the data, replay one |
| Scattered, no pattern | Genuine flakiness | Investigate the dependency's own error rate |

**5. Replay only after the cause is fixed.** Replaying into an unfixed
consumer just refills the DLQ and doubles the noise.

```bash
# Snapshot first — a replay that goes wrong should be recoverable.
gcloud pubsub snapshots create dlq-triage-$(date +%Y%m%d) \
    --subscription=nimbusfs-file-events-dlq-sub

# Seek the *source* subscription back to before the failure window, so
# the normal consumer path re-processes. Consumer idempotency makes this
# safe for anything that actually succeeded in the interim.
gcloud pubsub subscriptions seek nimbusfs-file-events-file-worker-sub \
    --time=2026-08-18T00:00:00Z
```

**6. If it can never succeed**, it should not have been in the DLQ.
Record the decision (`processed_events` row with a FAILED status and an
explanatory error), purge, and — more importantly — fix the
classification in `app/workers/` so the next one is ACKed and recorded
rather than retried five times first.

**Phase 8 ships none of this as tooling.** There is no replay script, no
DLQ dashboard, and nothing cross-region. These are documented `gcloud`
steps a human runs.

---

## 7. Ordering

### 7.1 The decision: no ordering keys in Phase 8

Pub/Sub can guarantee ordered delivery per ordering key. NimbusFS does
not use it, and the decision was made by auditing the actual event
catalog rather than by defaulting:

| Consumer | Needs order? | Why not |
|---|---|---|
| file-processing | No | Validates and forwards; reads current storage state. |
| thumbnail | No | Renders from the object's *current* bytes. Two `thumbnail.requested` for one file in any order produce the same deterministic object. |
| notification | No | Append-only rows; two notifications are two rows regardless of order. |

The cost of enabling it anyway would be real: ordered delivery serializes
per key, capping throughput at one in-flight message per aggregate, and
turns one stuck message into a **head-of-line block** for every
subsequent message with the same key. That is a serious availability
trade, and it should be paid only when something needs it.

### 7.2 What would need it, and how it would be turned on

A consumer that maintains **derived state from a sequence** — a
materialized per-folder file count, an audit trail that must read in
order, a replicated read model. If one arrives:

1. Set `ordering_key=str(aggregate_id)` in `EventPublisher.publish`.
   `aggregate_id` is already captured on every `OutboxEvent`, so there is
   **no migration**.
2. Enable message ordering on the subscription.
3. Publish through a single publisher client with ordering enabled (order
   is only guaranteed per publisher, per region, per key).
4. Accept head-of-line blocking, and pair it with an explicit poison-
   message policy so one bad message cannot stall an aggregate forever.

Note also that the outbox's `ORDER BY created_at` gives best-effort
chronological *publishing*, not ordered *delivery* — rows are published
independently and Pub/Sub without ordering keys does not preserve order.
Best-effort ordering that nobody depends on is fine; best-effort ordering
that somebody depends on is a latent bug.

---

## 8. Versioning

`event_version` starts at 1 per event type.

**Additive changes do not bump it.** Adding an optional `payload` key is
backward compatible because consumers ignore unknown keys — `payload` is
a plain dict and nothing forbids extras. A new event *type* is likewise
additive: existing consumers filter on `event_type` via `interested_in()`
and decline what they do not handle.

**A version bump is reserved for a breaking change**: removing a payload
key, renaming one, or changing a value's type or meaning. The rollout is
the standard one and the ordering is not optional:

1. Deploy consumers that understand **both** v1 and v2.
2. Then start publishing v2 (in parallel with v1 if consumers are mixed).
3. Drain v1 from every subscription.
4. Then remove v1 handling.

Doing (2) before (1) breaks every consumer at once.

`event_version` is exposed as a Pub/Sub **attribute**, not only inside
the payload, so a subscription filter can route versions to different
consumers without deserializing. That is also why `event_type` and
`correlation_id` are attributes: server-side filtering later is a config
change, not a code change.

A consumer that receives a version it does not understand must treat it
as **non-retryable** — ACK, record FAILED, alert — rather than
crash-loop. A crash-looping consumer on an unknown version takes the
whole subscription down with it, which converts a rollout-ordering
mistake into an outage.

There is a deliberate exception, and it is worth noticing because it goes
the other way: an unknown **`notification_type`** falls back to a generic
template rather than failing. The difference is that a notification type
is *data* in a known-version envelope, not a contract change — dropping
the notification or crash-looping on it would be a worse answer than a
slightly generic subject line.

---

## 9. Failure catalogue

| # | Failure | Behavior | Data loss? | Recovery |
|---|---|---|---|---|
| 1 | **Pub/Sub unavailable** | API unaffected (writes are Postgres-only). Rows accumulate FAILED with exponential backoff to a 300s cap. | None | Automatic on recovery |
| 2 | **Publisher crashes after publish, before commit** | Row stays PENDING; republished next poll. | None (duplicate instead) | Automatic; consumer dedup absorbs it |
| 3 | **Publisher crashes between rows** | Per-row commit means completed rows stay PUBLISHED; the rest stay PENDING. | None | Automatic on restart |
| 4 | **Publisher process dies entirely** | Rows accumulate PENDING. Latency, not loss. Kubernetes restarts it. | None | Automatic; alarm on PENDING age |
| 5 | **Worker crashes mid-message** | No ack; redelivered after the deadline; reprocessed. | None | Automatic |
| 6 | **Worker crashes after work, before ack** | Redelivered; the pre-check finds the ledger row (work+ledger committed together) and skips. | None | Automatic |
| 7 | **Postgres unavailable** | API is down regardless (it is authoritative). Workers NACK everything; backlog grows. | None | Automatic on recovery |
| 8 | **GCS unavailable** | Thumbnail/file workers NACK (retryable). Backlog grows. | None | Automatic |
| 9 | **GCS object missing** | Non-retryable: ACK + `ProcessedEvent(FAILED)` naming the object. | The thumbnail, permanently | Human: investigate why bytes vanished |
| 10 | **Duplicate event** | Pre-check skips, or the unique constraint rejects and the loser still ACKs. | None | Automatic |
| 11 | **Invalid / unparseable event** | Logged at ERROR, ACKed. No ledger row (no `event_id` to key one on). | That one message | Human: read the log, fix the producer |
| 12 | **Poison message (valid envelope, always fails)** | If classified retryable: retried to `MAX_DELIVERY_ATTEMPTS`, then dead-lettered. If non-retryable: ACKed with a FAILED row immediately. | None | Human: DLQ runbook §6.3 |
| 13 | **Ledger write fails after successful work** | NACK. Redelivery re-runs `process()` — safe by contract. | None | Automatic |
| 14 | **Two publisher replicas** | `FOR UPDATE SKIP LOCKED` prevents double-claiming. Safe, though not obviously useful. | None | n/a |
| 15 | **Thumbnail OOM (decode bomb)** | Pod OOM-killed; message never acked; redelivered — potentially to another pod that also dies. | None, but a possible loop | Human: the allow-list and pinned decoder are the real defenses; the memory limit only bounds blast radius |
| 16 | **Emulator/topic missing (local dev)** | Publish raises; row marked FAILED and backs off. | None | Create the topic; rows drain |

Scenario 15 is the one with a genuinely unpleasant tail, and it is worth
being explicit rather than reassuring: a message that reliably OOM-kills
its consumer is redelivered to a fresh pod that also dies, and neither
`ProcessedEvent` nor the DLQ helps because the process never lives long
enough to classify anything. The defenses are all upstream — the
four-type allow-list checked before download, `Image.open(formats=...)`
pinning the decoder, and `MAX_DELIVERY_ATTEMPTS` eventually dead-lettering
it. Pillow's own `MAX_IMAGE_PIXELS` guard and an explicit pre-decode
dimension check would be the right next hardening step; Phase 8 does not
add them.

---

## 10. Interview questions

**Why not just publish after `session.commit()`?**
Because there is no transaction spanning Postgres and Pub/Sub, and a
crash in between loses the event *silently* — no error, no retry counter,
no queue depth to alarm on. The outbox makes the intent to publish part
of the same commit as the fact. §1.

**Doesn't the outbox just move the dual-write problem to the publisher?**
Yes, and that is the point. It moves it from "commit then publish" (crash
= silent loss) to "publish then mark published" (crash = duplicate). A
duplicate is absorbable by idempotent consumers; a silent loss is not
absorbable by anything.

**How do you get exactly-once?**
You do not. You get at-least-once delivery plus idempotent consumption,
which yields effectively-once *processing*. Anyone claiming exactly-once
delivery across a network is either wrong or redefining the term.

**Why is a unique constraint the guarantee and not the pre-check?**
Two replicas can both pass the pre-check in the same instant; only one
can win the insert. The pre-check saves work in the common case; the
constraint is what is actually true under concurrency.

**Why is the derived event ID deterministic?**
So a retried fan-out publishes the same child `event_id`s and downstream
dedup fires. With `uuid4()` every retry looks like a brand-new event and
thumbnails regenerate forever — with no error, no alarm, just cost. It is
the least visible bug in the design and there is a test dedicated to it.

**Why ACK non-retryable failures instead of dead-lettering them?**
Because they cannot succeed on redelivery. The DLQ is for retry-exhausted
messages a human might replay after a fix. Filling it with permanently
impossible work is how a DLQ becomes an ignored queue — and an ignored
DLQ is worse than no DLQ, because it looks like coverage.

**When would you use ordering keys?**
When a consumer maintains derived state from a *sequence*. None do today,
and ordering costs throughput plus head-of-line blocking. `aggregate_id`
is already on every outbox row, so enabling it later is a publisher
change with no migration. §7.

**Why three topics?**
Three genuine fan-out boundaries. One firehose couples every consumer's
backlog to every other's; twelve makes adding an event type a Terraform
change for no isolation benefit. `notification-events` is separate
specifically because it is an *egress* boundary — a wedged third party
must never apply backpressure to file processing.

**Why is the fan-out published directly instead of through the outbox?**
The outbox exists to make a Postgres write atomic with a publish. The
file worker performs no business write — with nothing to be atomic
*with*, an outbox row would add a table write, a poll interval of latency
and a second process's involvement, and buy nothing. A failed fan-out
NACKs and the whole step re-runs; deterministic child IDs make the repeat
harmless.

**Why does `EventPublisher` wrap the client in `run_in_executor`?**
`google-cloud-pubsub`'s publisher is synchronous and returns a
`concurrent.futures.Future`. Calling it directly from a coroutine blocks
the event loop for a network round trip, stalling every other request
that replica is serving. Two hops are needed: `run_in_executor` for the
call, `asyncio.wrap_future` for its return value. The fake in the test
suite returns a real `concurrent.futures.Future` precisely so that
skipping either hop fails a test.

**Why do workers have a liveness probe but no readiness probe?**
Readiness means "ready to receive traffic from a Service." Nothing routes
traffic to a worker — they pull. A readiness probe there could only mark
a healthy worker not-Ready and stall a rollout. Liveness is an exec probe
on a heartbeat file touched on a *timer*, not on message arrival: an idle
worker on an empty subscription is healthy, and it checks the file's
mtime rather than its existence so a wedged event loop is caught.

**How would you scale this?**
Workers scale horizontally on subscription backlog
(`num_undelivered_messages`) via a custom-metric HPA — designed, not
built. The publisher is a poller and scales poorly by replica count;
`SKIP LOCKED` makes more replicas *safe*, but throughput there is bounded
by the database and the publish RPC, so the honest answer is "batch
harder, do not add replicas."

**What breaks first under load?**
The thumbnail worker — it is the only CPU/memory-bound component. Its
concurrency is deliberately 3 rather than the shared default of 10,
because its ceiling is RAM, not network waits. The next thing to break
would be the outbox table itself, which grows unbounded (see below).

**What is missing?**
Backlog autoscaling; a real notification provider; DLQ replay tooling; an
outbox retention/archival job (the table only grows, and PUBLISHED rows
are never pruned — a real operational gap, not a hypothetical one);
Pillow pixel-count limits; and — most importantly — **none of Phase 8 has
ever been run against real infrastructure.** No emulator, no Pub/Sub, no
Postgres, no Docker, no cluster. Every claim in this document is
justified by design reasoning and a test suite running against in-memory
SQLite and hand-written fakes.
