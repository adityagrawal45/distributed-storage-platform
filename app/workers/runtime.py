"""
Shared worker runtime concerns: graceful shutdown and the liveness
heartbeat.

These are the two things every worker needs and nothing else has in
common, which is why they live in a small mixin here rather than in
`BaseWorker` (the outbox publisher is not a Pub/Sub consumer but still
needs both).

Graceful shutdown
-----------------
Kubernetes terminates a pod by sending SIGTERM and then waiting
`terminationGracePeriodSeconds` before SIGKILL. A worker that ignores
SIGTERM gets killed mid-message; a worker that exits *immediately* on
SIGTERM abandons in-flight work. Both are survivable here — consumers are
idempotent and unacked messages are redelivered — but both cost a
redelivery and a confusing log. So: SIGTERM/SIGINT set an
`asyncio.Event`, the main loop notices and stops accepting new work, and
the process waits up to `WORKER_SHUTDOWN_GRACE_SECONDS` for what is
already in flight. The bound matters: an unbounded wait converts a stuck
handler into a pod that never terminates and eventually gets SIGKILLed
anyway, just later and less predictably.

Liveness heartbeat
------------------
The probe target is a file touched on a **timer**, deliberately
independent of message arrival. The tempting alternative — "healthy if we
processed a message recently" — is wrong: an idle worker on an empty
subscription is perfectly healthy, and that probe would restart every
worker during quiet periods, which is both useless churn and a great way
to turn a quiet night into a CrashLoopBackOff alert.

There is deliberately **no readiness probe** for workers. Readiness means
"ready to receive traffic from a Service"; no Service selects worker
pods, because workers pull from Pub/Sub rather than being pushed to. A
readiness probe there would be answering a question nobody asks.
"""

import asyncio
import os
import signal
from pathlib import Path

from app.core.config import get_settings
from app.logging.logger import get_logger

logger = get_logger(__name__)


class WorkerRuntimeMixin:
    """Signal handling + heartbeat, shared by every worker process."""

    worker_name: str = "worker"

    def _init_runtime(self) -> None:
        """
        Idempotent — every worker calls this from `__init__` so
        `shutting_down`/`request_shutdown` are usable immediately (tests
        drive a single iteration without ever calling `run()`), and
        `run()` calls it again defensively without resetting state.
        """
        if getattr(self, "_runtime_initialized", False):
            return
        settings = get_settings()
        self._runtime_initialized = True
        self._shutdown_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_interval = settings.WORKER_HEARTBEAT_INTERVAL_SECONDS
        self._heartbeat_path = settings.WORKER_HEARTBEAT_FILE_PATH
        self._shutdown_grace = settings.WORKER_SHUTDOWN_GRACE_SECONDS

    @property
    def shutting_down(self) -> bool:
        return self._shutdown_event.is_set()

    def request_shutdown(self) -> None:
        """Idempotent — a second SIGTERM while draining must not double-handle."""
        if not self._shutdown_event.is_set():
            logger.info("worker_shutdown_requested", worker=self.worker_name)
            self._shutdown_event.set()

    def install_signal_handlers(self) -> None:
        """
        Registers SIGTERM/SIGINT via the event loop where possible.

        `loop.add_signal_handler` is not implemented on Windows, and there
        is no signal handling at all off the main thread — both raise
        `NotImplementedError`/`ValueError`. Neither case is a reason to
        refuse to start (a worker run under a test harness or on a dev
        laptop is still useful), so the failure is logged and the worker
        runs without cooperative shutdown.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
            except (NotImplementedError, ValueError, AttributeError, RuntimeError):
                logger.warning(
                    "worker_signal_handler_unavailable", worker=self.worker_name, signal=str(sig)
                )

    # -- heartbeat -----------------------------------------------------
    def touch_heartbeat(self) -> None:
        """
        Writes/updates the liveness file. Best effort: a full or read-only
        filesystem must not crash a worker that is otherwise doing its
        job — the probe failing is a *symptom* reporting mechanism, and
        turning it into the failure itself would be backwards.
        """
        try:
            path = Path(self._heartbeat_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            os.utime(path, None)
        except (OSError, ValueError) as exc:
            # ValueError covers a structurally invalid path (embedded
            # NUL); OSError covers a read-only or full filesystem.
            logger.warning("worker_heartbeat_write_failed", worker=self.worker_name, error=str(exc))

    async def _heartbeat_loop(self) -> None:
        while not self._shutdown_event.is_set():
            self.touch_heartbeat()
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._heartbeat_interval)
            except asyncio.TimeoutError:
                continue

    def start_heartbeat(self) -> None:
        self.touch_heartbeat()  # so the probe passes immediately, not one interval later
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeat(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - cancellation is the expected path
                pass
            self._heartbeat_task = None

    async def sleep_or_shutdown(self, seconds: float) -> None:
        """
        Sleeps, but wakes immediately on shutdown.

        A plain `asyncio.sleep(poll_interval)` would make SIGTERM take up
        to a full poll interval to be noticed — small, but it is the
        difference between a clean rolling update and one that looks
        stalled.
        """
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
