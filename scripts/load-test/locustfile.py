"""
Locust load test for Phase 6 chunked/resumable uploads — alternative to
`k6-chunked-upload.js` for teams that prefer a Python-based load tool.
Covers the same lifecycle (register/login -> initiate -> parallel chunk
upload -> resume simulation -> complete -> download-verify) with less
built-in parallelism control than k6's `http.batch` (Locust users are
inherently one-request-at-a-time per simulated user unless you spawn
`gevent` greenlets yourself), so k6 is the RECOMMENDED tool for this
specific "parallel chunk upload" scenario — this file exists for teams
already standardized on Locust, not as the primary artifact.

Run:
    pip install locust
    locust -f scripts/load-test/locustfile.py --host http://localhost:8000
    # then open http://localhost:8089 and start a run (e.g. 100 users, 10/s spawn rate)
"""

import os
import random
import time
import uuid

import gevent
from locust import HttpUser, between, task

CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", 256 * 1024))
TOTAL_CHUNKS = int(os.environ.get("TOTAL_CHUNKS", 8))
FAIL_CHUNK_RATE = float(os.environ.get("FAIL_CHUNK_RATE", 0.05))
RESUME_RATE = float(os.environ.get("RESUME_RATE", 0.2))


class ChunkedUploadUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        email = f"locust-{uuid.uuid4()}@nimbusfs.load"
        password = "LoadTest!2345"
        self.client.post(
            "/api/v1/auth/register",
            json={"first_name": "Locust", "last_name": "Test", "email": email, "password": password},
        )
        login = self.client.post(
            "/api/v1/auth/login", data={"username": email, "password": password}
        )
        self.token = login.json()["data"]["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def _upload_chunk(self, upload_id: str, chunk_number: int, size: int, corrupt: bool = False):
        payload = os.urandom(size - 10 if corrupt else size)
        return self.client.put(
            f"/api/v1/uploads/{upload_id}/chunks/{chunk_number}",
            data=payload,
            headers=self.headers,
            name="/api/v1/uploads/[id]/chunks/[n]",
        )

    def _upload_chunk_with_retry(self, upload_id: str, chunk_number: int, size: int):
        corrupt = random.random() < FAIL_CHUNK_RATE
        response = self._upload_chunk(upload_id, chunk_number, size, corrupt=corrupt)
        if corrupt and response.status_code != 200:
            time.sleep(0.2)
            response = self._upload_chunk(upload_id, chunk_number, size, corrupt=False)
        return response

    @task
    def chunked_upload_lifecycle(self):
        total_size = CHUNK_SIZE * (TOTAL_CHUNKS - 1) + CHUNK_SIZE // 2
        initiate = self.client.post(
            "/api/v1/uploads",
            json={
                "filename": f"locust-{uuid.uuid4()}.bin",
                "size": total_size,
                "mime_type": "application/octet-stream",
                "chunk_size": CHUNK_SIZE,
            },
            headers=self.headers,
        )
        if initiate.status_code != 201:
            return
        session = initiate.json()["data"]
        upload_id = session["upload_id"]

        simulate_resume = random.random() < RESUME_RATE
        chunks_this_pass = (TOTAL_CHUNKS + 1) // 2 if simulate_resume else TOTAL_CHUNKS

        # Genuine parallelism via gevent greenlets — the same "many
        # chunks in flight at once" load k6's http.batch produces.
        def _size_for(n: int) -> int:
            return CHUNK_SIZE if n < TOTAL_CHUNKS else total_size - CHUNK_SIZE * (TOTAL_CHUNKS - 1)

        jobs = [
            gevent.spawn(self._upload_chunk_with_retry, upload_id, n, _size_for(n))
            for n in range(1, chunks_this_pass + 1)
        ]
        gevent.joinall(jobs)

        if simulate_resume:
            time.sleep(0.5)
            status = self.client.get(f"/api/v1/uploads/{upload_id}", headers=self.headers)
            missing = status.json().get("data", {}).get("missing_chunks", [])
            jobs = [
                gevent.spawn(self._upload_chunk_with_retry, upload_id, n, _size_for(n)) for n in missing
            ]
            gevent.joinall(jobs)

        complete = self.client.post(f"/api/v1/uploads/{upload_id}/complete", headers=self.headers)
        if complete.status_code != 200:
            return

        file_id = complete.json()["data"]["file"]["id"]
        self.client.get(f"/api/v1/files/{file_id}/download", headers=self.headers, name="/api/v1/files/[id]/download")
