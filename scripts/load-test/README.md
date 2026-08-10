# Phase 6 Load Testing — Chunked Uploads

Two equivalent scripts exercising the full chunked-upload lifecycle
under concurrency: **k6** (`k6-chunked-upload.js`, recommended — native
parallel-request support via `http.batch`) and **Locust**
(`locustfile.py`, for teams standardized on Python/Locust already).

Both simulate: register/login → initiate a chunked upload → upload
chunks **in parallel** (with a configurable fraction deliberately
malformed to exercise retry) → for a configurable fraction of runs,
simulate a dropped connection and **resume** (query missing chunks,
upload only those) → complete → verify the resulting file downloads
correctly.

## Running k6

```bash
# Install: https://k6.io/docs/get-started/installation/
k6 run scripts/load-test/k6-chunked-upload.js \
  -e BASE_URL=http://localhost:8000

# Override the load profile / chunk shape:
k6 run scripts/load-test/k6-chunked-upload.js \
  -e BASE_URL=http://localhost:8000 \
  -e CHUNK_SIZE=1048576 \
  -e TOTAL_CHUNKS=20 \
  -e FAIL_CHUNK_RATE=0.1 \
  -e RESUME_RATE=0.3
```

Default profile: ramps 0 → 20 → 100 concurrent virtual users over ~5
minutes (see `options.scenarios` in the script), holding at 100 for 3
minutes — matching the "100 concurrent users" requirement.

## Running Locust

```bash
pip install locust
locust -f scripts/load-test/locustfile.py --host http://localhost:8000
# open http://localhost:8089, set 100 users / 10 per-second spawn rate, start
```

## What to observe

| Metric | Where | What it tells you |
|---|---|---|
| `nimbusfs_initiate_duration` (k6) / `/api/v1/uploads` (Locust UI) | Custom trend | Postgres write latency under load — should stay flat as VUs scale (it's one small INSERT) |
| `nimbusfs_chunk_upload_duration` | Custom trend | GCS write latency + app overhead per chunk — the metric most likely to degrade under real concurrency, since it's the one doing real I/O to storage |
| `nimbusfs_complete_duration` | Custom trend | Compose + checksum-streaming cost — expect this to scale with `TOTAL_CHUNKS`, since `compute_object_checksum` reads the whole composed object once |
| `nimbusfs_chunk_retries` / `nimbusfs_upload_failures` | Custom counters | Retry behavior is working (`chunk_retries` > 0, but `upload_failures` stays near 0 — a retry succeeding is not a failure) |
| `nimbusfs_resumed_uploads` | Custom counter | Confirms the resume path actually ran and completed successfully |
| `http_req_failed` | Built-in | Overall hard-failure rate — the `thresholds` block fails the run if this exceeds 2% |
| App logs (`chunk_upload_started`/`chunk_upload_completed`/`upload_completing`, structured JSON — Phase 4) | `kubectl logs` / `docker compose logs` | Cross-reference slow requests against `duration_ms` and `server_id` to see whether load is skewed toward specific replicas (it shouldn't be — Phase 4/5 statelessness) |
| Postgres connection pool usage | `pg_stat_activity`, or Cloud SQL metrics | Chunk uploads are DB-light (one row read + one row write per chunk) — this should NOT be the bottleneck; if it is, something is holding a transaction open longer than intended (see `ChunkedUploadService`'s "short transactions" design note) |
| GCS request latency/errors | Cloud Monitoring (`storage.googleapis.com`) | The actual storage-backend cost of N parallel chunk PUTs + the Compose calls at completion |

## What NOT to conclude from this

- **This is not a bandwidth benchmark.** Chunk size and count here are
  small (256 KiB × 8 by default, a few MB per simulated upload) so the
  test focuses on REQUEST THROUGHPUT and CORRECTNESS under concurrency
  (does the server handle 100 concurrent multi-chunk uploads without
  corrupting state?), not on how many GB/s a single upload can sustain
  — that's dominated by client network conditions this test doesn't
  simulate.
- **A local/dev run is not a production capacity plan.** Real GCS
  latency, real Cloud SQL connection limits, and real network hops
  between GKE pods and managed services will all differ from a
  `docker-compose` or single-machine run. Treat results here as
  relative (did this change make things worse?) not absolute (this
  system handles exactly N requests/sec in production).
- **Don't run this against a shared staging environment other people
  depend on** without coordinating first — 100 concurrent users each
  running multi-chunk uploads is real, sustained load.

## Kubernetes-specific verification

To specifically confirm resumable uploads survive pod distribution and
pod failure (not something a load-testing tool alone proves), pair a
load-test run with `scripts/k8s-smoke-test.sh --full`'s self-healing
demo (Phase 5): start a chunked upload, let a few chunks land, then
delete a Pod mid-run and confirm the upload still completes — see the
main README's Phase 6 "Kubernetes Behavior" section for the full
walkthrough and why this works (Postgres, not any one Pod, is
authoritative for upload state).
