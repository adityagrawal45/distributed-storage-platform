#!/usr/bin/env python3
"""
Cache A/B benchmark for NimbusFS read-heavy endpoints (Phase 7).

Measures end-to-end HTTP latency for the same read endpoints twice — once
with `CACHE_ENABLED=true` and once with `CACHE_ENABLED=false` — against a
RUNNING NimbusFS instance, and prints a side-by-side comparison.

IMPORTANT — no numbers are baked into this repository
-----------------------------------------------------
This script produces measurements; it does not ship any. Nothing in the
Phase 7 documentation quotes a speedup figure, because a cache's benefit
depends entirely on your Postgres, your Redis, your network, and your
data shape — a number measured on a laptop against SQLite would be
actively misleading if quoted as "NimbusFS is Nx faster". Run it in an
environment you care about and read your own numbers.

How the A/B works
-----------------
`CACHE_ENABLED` is read once per process by `get_settings()` (it is
`lru_cache`d), so the two arms cannot be toggled at runtime over HTTP.
The script therefore does NOT try to flip it: you run it twice, once
against a server started with each setting, and it writes a JSON result
file each time. Pass `--compare a.json b.json` to print the comparison
table. This is deliberately explicit rather than clever — a benchmark
that silently measured a half-warm process would be worse than none.

What it measures
----------------
Three read paths, chosen because they are the ones Phase 7 actually
caches and because they have very different cost profiles:

  * `GET /folders/{id}`            — one indexed row lookup (cheapest;
                                     shows the cache's *floor*, i.e. how
                                     much is pure network/serialization)
  * `GET /folders/breadcrumb`      — one query PER ANCESTOR in the
                                     uncached path, so it should show the
                                     largest relative improvement
  * `GET /metadata/search?q=...`   — a filtered query plus a COUNT

Latency is reported as p50/p90/p99 and mean, from `time.perf_counter()`
around each request. Percentiles matter far more than the mean here: a
cache's real value is usually in shrinking the tail, and a mean can look
flat while p99 halves.

Usage
-----
    # Terminal 1 — cached arm
    CACHE_ENABLED=true uvicorn app.main:app --port 8000

    # Terminal 2
    python scripts/benchmark/benchmark_cache.py \
        --base-url http://localhost:8000 --label cached --out cached.json

    # Then restart the server with CACHE_ENABLED=false and:
    python scripts/benchmark/benchmark_cache.py \
        --base-url http://localhost:8000 --label uncached --out uncached.json

    python scripts/benchmark/benchmark_cache.py --compare cached.json uncached.json

See scripts/benchmark/README.md for the full runbook and for what NOT to
conclude from the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field

try:
    import httpx
except ImportError:  # pragma: no cover - operator-facing script
    print("This script needs httpx:  pip install httpx", file=sys.stderr)
    raise SystemExit(1)


DEFAULT_PASSWORD = "StrongP@ssw0rd"


@dataclass
class Measurement:
    name: str
    samples_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        if not self.samples_ms:
            return {"name": self.name, "n": 0}
        ordered = sorted(self.samples_ms)

        def pct(p: float) -> float:
            # Nearest-rank percentile: no interpolation, so a reported
            # p99 is always an actually-observed request, not an
            # arithmetic artifact between two of them.
            index = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered))) - 1))
            return ordered[index]

        return {
            "name": self.name,
            "n": len(ordered),
            "mean_ms": round(statistics.fmean(ordered), 3),
            "p50_ms": round(pct(50), 3),
            "p90_ms": round(pct(90), 3),
            "p99_ms": round(pct(99), 3),
            "min_ms": round(ordered[0], 3),
            "max_ms": round(ordered[-1], 3),
        }


class Benchmark:
    def __init__(self, base_url: str, iterations: int, warmup: int, depth: int):
        self.base_url = base_url.rstrip("/")
        self.iterations = iterations
        self.warmup = warmup
        self.depth = depth
        self.api = f"{self.base_url}/api/v1"

    async def _register_and_login(self, http: httpx.AsyncClient) -> str:
        email = f"bench-{uuid.uuid4().hex[:12]}@nimbusfs.io"
        await http.post(
            f"{self.api}/auth/register",
            json={
                "first_name": "Bench",
                "last_name": "Mark",
                "email": email,
                "password": DEFAULT_PASSWORD,
            },
        )
        login = await http.post(
            f"{self.api}/auth/login", data={"username": email, "password": DEFAULT_PASSWORD}
        )
        login.raise_for_status()
        return login.json()["data"]["access_token"]

    async def _seed(self, http: httpx.AsyncClient) -> dict:
        """Creates a nested folder chain and a handful of files to read back."""
        parent_id = None
        deepest_id = None
        for level in range(self.depth):
            payload = {"name": f"level-{level}-{uuid.uuid4().hex[:6]}"}
            if parent_id:
                payload["parent_folder_id"] = parent_id
            response = await http.post(f"{self.api}/folders", json=payload)
            response.raise_for_status()
            parent_id = deepest_id = response.json()["data"]["id"]

        for index in range(10):
            await http.post(
                f"{self.api}/metadata",
                json={
                    "original_filename": f"benchmark-report-{index}.pdf",
                    "mime_type": "application/pdf",
                    "size": 1024 * (index + 1),
                    "folder_id": deepest_id,
                },
            )

        return {"folder_id": deepest_id}

    async def _time_get(self, http: httpx.AsyncClient, url: str, params: dict | None = None) -> float:
        started = time.perf_counter()
        response = await http.get(url, params=params)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if response.status_code == 429:
            # Benchmarks trip rate limits easily. Say so loudly rather
            # than silently reporting the (very fast) 429 latency as if it
            # were a real read.
            raise RuntimeError(
                "Hit a 429. Raise RATE_LIMIT_METADATA_REQUESTS / "
                "RATE_LIMIT_SEARCH_REQUESTS, or set RATE_LIMIT_ENABLED=false, "
                "for the duration of the benchmark."
            )
        response.raise_for_status()
        return elapsed_ms

    async def run(self, label: str) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as http:
            token = await self._register_and_login(http)
            http.headers["Authorization"] = f"Bearer {token}"

            seeded = await self._seed(http)
            folder_id = seeded["folder_id"]

            targets = [
                ("folder_metadata", f"{self.api}/folders/{folder_id}", None),
                ("folder_breadcrumb", f"{self.api}/folders/breadcrumb", {"folder_id": folder_id}),
                ("file_search", f"{self.api}/metadata/search", {"q": "benchmark", "page_size": 20}),
            ]

            results: list[dict] = []
            for name, url, params in targets:
                # Warm-up requests are discarded: the first call to any
                # endpoint pays connection setup, SQLAlchemy statement
                # compilation, and (in the cached arm) the population
                # miss. Including them would measure startup, not steady
                # state — and would flatter the uncached arm by hiding
                # the cached arm's one-time population cost inside p50.
                for _ in range(self.warmup):
                    await self._time_get(http, url, params)

                measurement = Measurement(name)
                for _ in range(self.iterations):
                    measurement.samples_ms.append(await self._time_get(http, url, params))
                results.append(measurement.summary())
                print(f"  {name:<20} {json.dumps(results[-1])}")

            return {
                "label": label,
                "base_url": self.base_url,
                "iterations": self.iterations,
                "warmup": self.warmup,
                "folder_depth": self.depth,
                "endpoints": results,
            }


def print_comparison(a: dict, b: dict) -> None:
    """Prints a side-by-side table. Which arm is which is up to the operator."""
    by_name_a = {e["name"]: e for e in a["endpoints"]}
    by_name_b = {e["name"]: e for e in b["endpoints"]}

    header = f"{'endpoint':<20} {'metric':<8} {a['label']:>12} {b['label']:>12} {'delta':>10}"
    print(header)
    print("-" * len(header))
    for name in by_name_a:
        if name not in by_name_b:
            continue
        for metric in ("p50_ms", "p90_ms", "p99_ms", "mean_ms"):
            left = by_name_a[name].get(metric)
            right = by_name_b[name].get(metric)
            if left is None or right is None:
                continue
            delta = "n/a" if right == 0 else f"{(left - right) / right * 100:+.1f}%"
            print(f"{name:<20} {metric:<8} {left:>12.3f} {right:>12.3f} {delta:>10}")
    print(
        "\nRead these as measurements of YOUR environment only. A cache that "
        "shows little benefit here may still be the difference between\n"
        "surviving and not surviving a traffic spike — this measures latency "
        "at low concurrency, not database load shed under it."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--depth", type=int, default=8, help="Folder nesting depth (drives breadcrumb cost).")
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", default=None, help="Write results as JSON to this path.")
    parser.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"), help="Compare two result files.")
    args = parser.parse_args()

    if args.compare:
        with open(args.compare[0], encoding="utf-8") as handle_a, open(
            args.compare[1], encoding="utf-8"
        ) as handle_b:
            print_comparison(json.load(handle_a), json.load(handle_b))
        return 0

    print(f"Benchmarking {args.base_url} (label={args.label}, iterations={args.iterations})")
    benchmark = Benchmark(args.base_url, args.iterations, args.warmup, args.depth)
    try:
        results = asyncio.run(benchmark.run(args.label))
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"Benchmark aborted: {exc}", file=sys.stderr)
        return 1

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2)
        print(f"\nWrote {args.out}")
    else:
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
