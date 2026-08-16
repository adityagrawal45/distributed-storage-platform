"""
In-memory fake of the `google.cloud.pubsub_v1` surface NimbusFS actually
uses. Same philosophy as `tests/fakes/fake_gcs.py` and
`tests/fakes/fake_redis.py`: a hand-written fake that really *stores*
messages in per-topic queues, so a test can assert "this exact envelope
landed on this exact topic" rather than "publish was called once".

Deliberately implements only what NimbusFS calls — this is not a Pub/Sub
emulator. In particular there is no ack-deadline timer, no automatic
redelivery, no ordering-key machinery and no dead-letter routing; those
are Pub/Sub *server* behaviors, and faking them would be inventing
semantics rather than testing ours. Where a test needs redelivery, it
hands the same message to the worker twice explicitly — which is both
more honest and a stronger test, since it proves idempotency against a
guaranteed duplicate instead of a probabilistic one.

Three pieces:

- `FakePublisherClient` — the publish side. Mirrors the real client's
  odd-but-important API shape: `publish()` returns a concurrent.futures
  `Future` (NOT an asyncio one), because that is exactly what
  `EventPublisher` has to bridge with `asyncio.wrap_future`. Faking it as
  a coroutine would let a real bug (blocking the event loop) pass.
- `FakeMessage` — the ack/nack recorder. `.ack()`/`.nack()` set flags a
  test asserts on, which is the entire observable contract between a
  worker and Pub/Sub.
- `fail_mode` / `start_failing()` — failure injection matching
  `FakeRedisClient`'s convention, so "Pub/Sub is unavailable" is a real
  assertion (the outbox row stays PENDING and backs off) rather than a
  claim.
"""

from __future__ import annotations

import uuid
from concurrent.futures import Future
from typing import Any


class FakePubSubFailure(RuntimeError):
    """
    What the fake raises in failure mode.

    A plain `RuntimeError` rather than a google-api-core exception: the
    production code paths deliberately catch broad `Exception` (an outbox
    row must back off no matter *how* the publish failed), so the exact
    type is not what is under test, and importing google's exception
    hierarchy into a fake would couple the tests to a client-library
    detail.
    """


class FakeMessage:
    """
    Stand-in for `google.cloud.pubsub_v1.subscriber.message.Message`.

    Records ack/nack instead of talking to a server. `delivery_attempt`
    is present because the real message carries it when a subscription
    has a dead-letter policy, and workers log it.
    """

    def __init__(
        self,
        data: bytes,
        attributes: dict[str, str] | None = None,
        *,
        message_id: str | None = None,
        delivery_attempt: int = 1,
    ):
        self.data = data
        self.attributes = attributes or {}
        self.message_id = message_id or str(uuid.uuid4())
        self.delivery_attempt = delivery_attempt

        self.acked = False
        self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True

    @property
    def settled(self) -> bool:
        """A message must always end up either acked or nacked — never neither, never both."""
        return self.acked != self.nacked


class FakePublisherClient:
    """
    Stand-in for `google.cloud.pubsub_v1.PublisherClient`.

    `topic_path()` and `publish()` are the only two methods NimbusFS
    calls. Published messages are retained per topic path so tests can
    inspect the full stream.
    """

    def __init__(self):
        # topic path -> list of (data_bytes, attributes)
        self.topics: dict[str, list[tuple[bytes, dict[str, str]]]] = {}
        self.publish_calls = 0

        # Failure injection (see module docstring).
        self.fail_mode = False
        self._fail_after = 0

    # -- test controls -------------------------------------------------
    def start_failing(self, *, after: int = 0) -> None:
        """After `after` more successful publishes, every publish raises."""
        self.fail_mode = True
        self._fail_after = after

    def stop_failing(self) -> None:
        self.fail_mode = False
        self._fail_after = 0

    def messages_on(self, topic_path: str) -> list[tuple[bytes, dict[str, str]]]:
        return self.topics.get(topic_path, [])

    def published_event_types(self) -> list[str]:
        """Every published message's `event_type` attribute, across all topics, in publish order."""
        return [attrs.get("event_type") for messages in self.topics.values() for _, attrs in messages]

    @property
    def total_published(self) -> int:
        return sum(len(v) for v in self.topics.values())

    # -- google-cloud-pubsub surface -----------------------------------
    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic_path: str, data: bytes, **attributes: Any) -> Future:
        """
        Returns a `concurrent.futures.Future`, exactly like the real
        client — resolved (or failed) immediately, since there is no
        network here. `EventPublisher` must bridge it with
        `asyncio.wrap_future`; if it ever awaited this directly the test
        would fail rather than silently blocking the event loop.
        """
        self.publish_calls += 1
        future: Future = Future()

        if self.fail_mode:
            if self._fail_after > 0:
                self._fail_after -= 1
            else:
                future.set_exception(FakePubSubFailure("fake pub/sub is unavailable"))
                return future

        string_attributes = {k: str(v) for k, v in attributes.items()}
        self.topics.setdefault(topic_path, []).append((data, string_attributes))
        future.set_result(f"fake-message-id-{self.publish_calls}")
        return future


class FakeSubscriberClient:
    """
    Minimal stand-in for `google.cloud.pubsub_v1.SubscriberClient`.

    Phase 8's worker tests drive `process()`/`_handle()` directly rather
    than through a streaming pull — a streaming pull is a background
    thread owned by the client library, and testing through it would be
    testing google's threading, not ours. This class exists so
    `BaseWorker.run()` can be constructed and shut down in a test without
    reaching the network.
    """

    def __init__(self):
        self.subscription_paths: list[str] = []
        self.closed = False
        self.cancelled = False

    def subscription_path(self, project: str, subscription: str) -> str:
        path = f"projects/{project}/subscriptions/{subscription}"
        self.subscription_paths.append(path)
        return path

    def subscribe(self, subscription_path: str, callback, flow_control=None):
        self._callback = callback
        return _FakeStreamingPullFuture(self)

    def close(self) -> None:
        self.closed = True

    def deliver(self, message: FakeMessage) -> None:
        """Hands a message to the registered callback, as the real streaming pull would."""
        self._callback(message)


class _FakeStreamingPullFuture:
    def __init__(self, client: FakeSubscriberClient):
        self._client = client

    def cancel(self) -> None:
        self._client.cancelled = True

    def result(self, timeout=None):  # pragma: no cover - shape parity only
        return None
