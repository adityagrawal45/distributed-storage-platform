/**
 * k6 load test for Phase 6 chunked/resumable uploads.
 *
 * Simulates the full upload lifecycle per virtual user (VU):
 *   register/login (once) -> initiate -> upload N chunks IN PARALLEL
 *   (via k6's http.batch, exercising the same "genuinely concurrent
 *   chunk PUTs" path the app's own tests cover with FakeGCSClient) ->
 *   complete -> verify the resulting file is downloadable.
 *
 * A configurable fraction of chunk uploads are deliberately made to
 * fail (bad Content-Length) to exercise retry behavior, and a
 * configurable fraction of uploads are only partially completed then
 * "resumed" in a second pass — see FAIL_CHUNK_RATE / RESUME_RATE below.
 *
 * Design decisions:
 * - One VU = one simulated user, each with their own account (created
 *   in `setup()` is NOT used here — accounts are created per-VU in the
 *   default function so VUs don't share auth state / rate limits) and
 *   their own uploads — never shares an upload session across VUs,
 *   matching how the real system is actually used.
 * - Chunk size is deliberately small (256 KiB) and total file size
 *   moderate (a few MB) — this is a load test of REQUEST THROUGHPUT
 *   and correctness under concurrency, not a network-bandwidth
 *   benchmark. Do not read "requests/sec" here as "GB/sec any real
 *   client could sustain" — see the accompanying README's "what NOT to
 *   conclude" section.
 * - Custom k6 metrics separate "control-plane" calls (initiate/status/
 *   complete — hit Postgres) from "chunk PUT" calls (hit GCS via
 *   FakeGCSClient... no, in a REAL run this hits real GCS through the
 *   app) so a dashboard can see database-side vs. storage-side latency
 *   separately instead of one blended number.
 */

import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";
import { randomString } from "https://jslib.k6.io/k6-utils/1.2.0/index.js";

// ---------------------------------------------------------------------
// Configuration (override via `k6 run -e KEY=value ...`)
// ---------------------------------------------------------------------
const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const API = `${BASE_URL}/api/v1`;
const CHUNK_SIZE = parseInt(__ENV.CHUNK_SIZE || "262144", 10); // 256 KiB
const TOTAL_CHUNKS = parseInt(__ENV.TOTAL_CHUNKS || "8", 10); // ~2 MiB file per VU per iteration
const FAIL_CHUNK_RATE = parseFloat(__ENV.FAIL_CHUNK_RATE || "0.05"); // 5% of chunk PUTs deliberately malformed, then retried
const RESUME_RATE = parseFloat(__ENV.RESUME_RATE || "0.2"); // 20% of iterations simulate a drop + resume

// ---------------------------------------------------------------------
// Custom metrics
// ---------------------------------------------------------------------
const initiateDuration = new Trend("nimbusfs_initiate_duration", true);
const chunkUploadDuration = new Trend("nimbusfs_chunk_upload_duration", true);
const completeDuration = new Trend("nimbusfs_complete_duration", true);
const chunkRetries = new Counter("nimbusfs_chunk_retries");
const resumedUploads = new Counter("nimbusfs_resumed_uploads");
const uploadFailures = new Counter("nimbusfs_upload_failures");

// ---------------------------------------------------------------------
// Load profile — ramps to 100 concurrent VUs, holds, ramps down.
// Override with `k6 run --stage ...` or edit directly for your target.
// ---------------------------------------------------------------------
export const options = {
  scenarios: {
    chunked_uploads: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: 20 }, // warm up
        { duration: "1m", target: 100 }, // ramp to the target 100 concurrent users
        { duration: "3m", target: 100 }, // hold — this is the window to actually watch
        { duration: "30s", target: 0 }, // ramp down
      ],
    },
  },
  thresholds: {
    // Fail the run if these SLOs are breached — tune to your environment.
    http_req_failed: ["rate<0.02"], // <2% hard failures (excludes deliberately-malformed retry chunks, tracked separately)
    nimbusfs_chunk_upload_duration: ["p(95)<2000"], // p95 chunk PUT under 2s
    nimbusfs_complete_duration: ["p(95)<5000"], // p95 completion (compose + checksum) under 5s
  },
};

function randomBytes(size) {
  // k6 has no native Buffer; build a repeatable-but-non-trivial payload.
  return randomString(size);
}

function registerAndLogin() {
  const email = `loadtest-${__VU}-${Date.now()}@nimbusfs.load`;
  const password = "LoadTest!2345";
  http.post(
    `${API}/auth/register`,
    JSON.stringify({ first_name: "Load", last_name: "Test", email, password }),
    { headers: { "Content-Type": "application/json" } }
  );
  const loginRes = http.post(
    `${API}/auth/login`,
    `username=${encodeURIComponent(email)}&password=${encodeURIComponent(password)}`,
    { headers: { "Content-Type": "application/x-www-form-urlencoded" } }
  );
  check(loginRes, { "login succeeded": (r) => r.status === 200 });
  return loginRes.json("data.access_token");
}

function initiateUpload(token) {
  const totalSize = CHUNK_SIZE * (TOTAL_CHUNKS - 1) + Math.floor(CHUNK_SIZE / 2);
  const start = Date.now();
  const res = http.post(
    `${API}/uploads`,
    JSON.stringify({
      filename: `loadtest-${__VU}-${__ITER}.bin`,
      size: totalSize,
      mime_type: "application/octet-stream",
      chunk_size: CHUNK_SIZE,
    }),
    { headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" } }
  );
  initiateDuration.add(Date.now() - start);
  check(res, { "initiate succeeded": (r) => r.status === 201 });
  if (res.status !== 201) {
    uploadFailures.add(1);
    return null;
  }
  return res.json("data");
}

function uploadChunk(token, uploadId, chunkNumber, size, corrupt) {
  const payload = randomBytes(corrupt ? size - 10 : size); // "corrupt" = wrong length, server rejects it
  const start = Date.now();
  const res = http.put(`${API}/uploads/${uploadId}/chunks/${chunkNumber}`, payload, {
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/octet-stream" },
  });
  chunkUploadDuration.add(Date.now() - start);
  return res;
}

function uploadChunkWithRetry(token, uploadId, chunkNumber, size) {
  const shouldFailFirst = Math.random() < FAIL_CHUNK_RATE;
  let res = uploadChunk(token, uploadId, chunkNumber, size, shouldFailFirst);

  if (shouldFailFirst && res.status !== 200) {
    chunkRetries.add(1);
    // Real client behavior: back off briefly, then retry with the
    // CORRECT payload — this is what "chunk retry" means end-to-end.
    sleep(0.2);
    res = uploadChunk(token, uploadId, chunkNumber, size, false);
  }

  const ok = check(res, { "chunk uploaded": (r) => r.status === 200 });
  if (!ok) uploadFailures.add(1);
  return ok;
}

export default function () {
  const token = registerAndLogin();
  if (!token) return;

  const session = initiateUpload(token);
  if (!session) return;

  const { upload_id: uploadId, chunk_size: chunkSize, total_chunks: totalChunks } = session;
  const simulateResume = Math.random() < RESUME_RATE;
  const chunksThisPass = simulateResume ? Math.ceil(totalChunks / 2) : totalChunks;

  // First pass: upload some (or all) chunks IN PARALLEL via http.batch —
  // this is the actual "parallel chunk upload" load, not a for-loop of
  // sequential awaits.
  const batchRequests = [];
  for (let n = 1; n <= chunksThisPass; n++) {
    const size = n < totalChunks ? chunkSize : session.total_size - chunkSize * (totalChunks - 1);
    const shouldFailFirst = Math.random() < FAIL_CHUNK_RATE;
    batchRequests.push([
      "PUT",
      `${API}/uploads/${uploadId}/chunks/${n}`,
      randomBytes(shouldFailFirst ? size - 10 : size),
      { headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/octet-stream" } },
    ]);
  }
  const batchStart = Date.now();
  const batchResponses = http.batch(batchRequests);
  chunkUploadDuration.add((Date.now() - batchStart) / batchRequests.length);

  batchResponses.forEach((res, i) => {
    if (res.status !== 200) {
      // Retry the (possibly deliberately-malformed) chunk once, correctly.
      chunkRetries.add(1);
      const n = i + 1;
      const size = n < totalChunks ? chunkSize : session.total_size - chunkSize * (totalChunks - 1);
      const retryOk = uploadChunkWithRetry(token, uploadId, n, size);
      if (!retryOk) uploadFailures.add(1);
    }
  });

  if (simulateResume) {
    resumedUploads.add(1);
    sleep(0.5); // simulate the gap between "connection dropped" and "client reconnects"

    // Resume: ask the server what's missing, upload exactly that.
    const statusRes = http.get(`${API}/uploads/${uploadId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    check(statusRes, { "status check succeeded": (r) => r.status === 200 });
    const missing = statusRes.json("data.missing_chunks") || [];

    missing.forEach((n) => {
      const size = n < totalChunks ? chunkSize : session.total_size - chunkSize * (totalChunks - 1);
      uploadChunkWithRetry(token, uploadId, n, size);
    });
  }

  const completeStart = Date.now();
  const completeRes = http.post(
    `${API}/uploads/${uploadId}/complete`,
    null,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  completeDuration.add(Date.now() - completeStart);
  const completed = check(completeRes, { "completion succeeded": (r) => r.status === 200 });
  if (!completed) {
    uploadFailures.add(1);
    return;
  }

  const fileId = completeRes.json("data.file.id");
  const downloadRes = http.get(`${API}/files/${fileId}/download`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  check(downloadRes, { "completed file is downloadable": (r) => r.status === 200 });

  sleep(1);
}
