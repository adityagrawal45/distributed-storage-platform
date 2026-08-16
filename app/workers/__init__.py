"""
Standalone background worker processes (Phase 8).

Every module in this package is runnable as `python -m app.workers.<name>`
and is a **separate process**, not a thread inside the FastAPI app. That
is the whole point of the phase: expensive, non-critical work
(thumbnailing, notification, post-upload validation) leaves the request
path entirely, so it can fail, retry, be scaled and be deployed
independently of the API — and so a thumbnail backlog can never make a
user's upload slow or a `POST /files/upload` return a 500.

Two shapes of worker live here:

- `outbox_publisher.py` polls **Postgres** and publishes to Pub/Sub. It
  is deliberately NOT a `BaseWorker` subclass: it consumes no
  subscription, has no ack/nack semantics and no idempotency ledger, and
  forcing it into the consumer base class would mean a base class of
  mostly-unused hooks. It shares only the runtime concerns that are
  genuinely common — graceful shutdown and the liveness heartbeat — via
  `runtime.py`.
- `file_processing_worker.py` / `thumbnail_worker.py` /
  `notification_worker.py` consume **Pub/Sub** and all subclass
  `base.BaseWorker`.
"""
