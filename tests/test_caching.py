"""
Phase 7 tests — distributed caching & coordination.

Covers, in order:
  1. CacheKeyBuilder      — naming, collision safety, fingerprint stability
  2. CacheSerializer      — round-tripping, schema versioning, refusals
  3. CacheService         — get/set/delete/exists/expire/increment/TTL/limits
  4. Stampede protection  — concurrent misses hit the source far fewer times
  5. Redis failure        — graceful degradation on every cache operation
  6. CacheInvalidator     — the key fan-out per operation
  7. Distributed locks    — acquisition, contention, expiry, ownership,
                            safe release, timeout, Redis failure
  8. End-to-end           — cache/DB consistency across the real HTTP API
                            for create/read/update/move/trash/restore/delete

Nothing here talks to a real Redis: `tests/fakes/fake_redis.py` is a real
in-memory implementation (including token-bucket arithmetic and a
controllable clock), and its failure-injection mode is what makes the
degradation assertions genuine rather than aspirational.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.core.cache.keys import CacheKeyBuilder
from app.core.cache.policy import CacheEntity, CachePolicy
from app.core.cache.serializer import CACHE_SCHEMA_VERSION, CacheSerializer
from app.core.config.settings import Settings
from app.core.distributed_lock import DistributedLock, DistributedLockFactory, DistributedLockService
from app.exceptions.custom_exceptions import (
    CacheSerializationError,
    DistributedLockError,
    LockAcquisitionTimeout,
    LockOwnershipError,
)
from app.services.cache_invalidator import CacheInvalidator
from app.services.cache_service import CacheService
from tests.fakes.fake_redis import FakeClock, FakeRedisClient

PREFIX = "nimbusfs"


def build_cache(client: FakeRedisClient, **setting_overrides) -> CacheService:
    settings = Settings(**setting_overrides)
    return CacheService(
        client,
        CachePolicy(settings),
        CacheKeyBuilder(settings.CACHE_KEY_PREFIX),
        enabled=settings.CACHE_ENABLED,
        lock_factory=DistributedLockFactory(client, default_ttl_seconds=5),
    )


# =====================================================================
# 1. CacheKeyBuilder
# =====================================================================
class TestCacheKeyBuilder:
    def test_keys_are_namespaced_and_entity_typed(self):
        keys = CacheKeyBuilder(PREFIX)
        entity_id = uuid.uuid4()
        assert keys.folder(entity_id) == f"nimbusfs:folder:{entity_id}"
        assert keys.file(entity_id) == f"nimbusfs:file:{entity_id}"
        assert keys.user(entity_id) == f"nimbusfs:user:{entity_id}"
        assert keys.file_versions(entity_id) == f"nimbusfs:file:{entity_id}:versions"
        assert keys.folder_breadcrumbs(entity_id) == f"nimbusfs:folder:{entity_id}:breadcrumbs"

    def test_same_id_different_entity_types_never_collide(self):
        keys = CacheKeyBuilder(PREFIX)
        shared_id = uuid.uuid4()
        assert keys.folder(shared_id) != keys.file(shared_id) != keys.user(shared_id)

    def test_root_children_key_is_owner_scoped_and_distinct_from_named_folder(self):
        keys = CacheKeyBuilder(PREFIX)
        owner = uuid.uuid4()
        root_key = keys.folder_children(None, owner, {"sort_by": "name"})
        named_key = keys.folder_children(uuid.uuid4(), owner, {"sort_by": "name"})
        assert root_key != named_key
        assert str(owner) in root_key

    def test_fingerprint_is_order_independent_but_value_sensitive(self):
        keys = CacheKeyBuilder(PREFIX)
        folder_id = uuid.uuid4()
        owner = uuid.uuid4()
        a = keys.folder_children(folder_id, owner, {"sort_by": "name", "sort_order": "asc"})
        b = keys.folder_children(folder_id, owner, {"sort_order": "asc", "sort_by": "name"})
        c = keys.folder_children(folder_id, owner, {"sort_by": "name", "sort_order": "desc"})
        assert a == b, "dict ordering must not produce two keys for one logical query"
        assert a != c, "a different sort order must produce a different key"

    def test_none_is_encoded_unambiguously(self):
        """`None` must not stringify to `"None"` and alias a real value."""
        keys = CacheKeyBuilder(PREFIX)
        owner = uuid.uuid4()
        folder = uuid.uuid4()
        real_none = keys.folder_children(folder, owner, {"is_deleted": None})
        literal_string = keys.folder_children(folder, owner, {"is_deleted": "None"})
        assert real_none != literal_string

        # And the root listing (folder_id=None) has its own structural
        # segment rather than a stringified None.
        root = keys.folder_children(None, owner, {})
        assert ":root:" in root and "None" not in root

    def test_search_key_is_user_scoped_before_the_hash(self):
        keys = CacheKeyBuilder(PREFIX)
        alice, bob = uuid.uuid4(), uuid.uuid4()
        params = {"q": "report", "page": 1}
        assert keys.search(alice, params) != keys.search(bob, params)
        assert keys.search(alice, params).startswith(f"nimbusfs:search:{alice}:")
        assert keys.search_pattern(alice) == f"nimbusfs:search:{alice}:*"

    def test_redact_is_stable_and_non_reversible(self):
        raw = "nimbusfs:search:abc:secretquery"
        assert CacheKeyBuilder.redact(raw) == CacheKeyBuilder.redact(raw)
        assert "secretquery" not in CacheKeyBuilder.redact(raw)


# =====================================================================
# 2. CacheSerializer
# =====================================================================
class TestCacheSerializer:
    def test_round_trips_the_types_our_schemas_actually_use(self):
        value = {
            "id": uuid.uuid4(),
            "created_at": datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
            "amount": Decimal("12.34"),
            "entity": CacheEntity.FOLDER,
            "tags": {"b", "a"},
        }
        hit, decoded = CacheSerializer.decode(CacheSerializer.encode(value))
        assert hit is True
        assert decoded["id"] == str(value["id"])
        assert decoded["created_at"] == "2026-08-15T12:00:00+00:00"
        assert decoded["amount"] == "12.34"
        assert decoded["entity"] == "folder"
        assert decoded["tags"] == ["a", "b"]

    def test_round_trips_a_pydantic_model(self):
        from app.schemas.folder import BreadcrumbItem

        item = BreadcrumbItem(id=uuid.uuid4(), name="Projects", path="/Projects")
        hit, decoded = CacheSerializer.decode(CacheSerializer.encode([item]))
        assert hit is True
        assert decoded[0]["name"] == "Projects"
        assert BreadcrumbItem.model_validate(decoded[0]).name == "Projects"

    def test_missing_value_is_a_miss_not_an_error(self):
        assert CacheSerializer.decode(None) == (False, None)

    def test_malformed_json_is_a_miss_not_an_error(self):
        assert CacheSerializer.decode("{not json at all") == (False, None)

    def test_unknown_schema_version_is_a_miss_not_a_crash(self):
        """A rolling deploy must never be able to crash on an old entry."""
        future = f'{{"v":{CACHE_SCHEMA_VERSION + 99},"ts":"2026-01-01T00:00:00","d":{{"a":1}}}}'
        assert CacheSerializer.decode(future) == (False, None)

    def test_bare_non_envelope_value_is_a_miss(self):
        assert CacheSerializer.decode('{"just":"a dict"}') == (False, None)

    def test_raw_bytes_are_refused(self):
        """File content belongs in GCS; the cache must never be a byte store."""
        with pytest.raises(CacheSerializationError):
            CacheSerializer.encode({"blob": b"\x00\x01"})

    def test_unserializable_object_raises_rather_than_writing_garbage(self):
        class NotSerializable:
            pass

        with pytest.raises(CacheSerializationError):
            CacheSerializer.encode(NotSerializable())

    def test_envelope_carries_a_write_timestamp(self):
        assert CacheSerializer.written_at(CacheSerializer.encode({"a": 1})) is not None


# =====================================================================
# 3. CacheService primitives
# =====================================================================
class TestCacheServicePrimitives:
    async def test_get_on_empty_cache_is_a_miss(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        assert await cache.get("nimbusfs:folder:missing") is None
        assert cache.misses == 1

    async def test_set_then_get_is_a_hit(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        assert await cache.set("k", {"name": "Projects"}, 60) is True
        assert await cache.get("k") == {"name": "Projects"}
        assert cache.hits == 1

    async def test_delete_removes_the_entry(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        await cache.set("k", {"a": 1}, 60)
        assert await cache.delete("k") == 1
        assert await cache.get("k") is None

    async def test_exists_reflects_presence(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        assert await cache.exists("k") is False
        await cache.set("k", 1, 60)
        assert await cache.exists("k") is True

    async def test_expire_resets_ttl_and_entry_disappears_when_it_lapses(self):
        clock = FakeClock()
        client = FakeRedisClient(clock=clock)
        cache = build_cache(client)
        await cache.set("k", {"a": 1}, 60)
        assert await cache.expire("k", 10) is True

        clock.advance(9)
        assert await cache.get("k") == {"a": 1}
        clock.advance(2)
        assert await cache.get("k") is None, "entry must vanish once its TTL lapses"

    async def test_ttl_from_set_is_honored(self):
        clock = FakeClock()
        cache = build_cache(FakeRedisClient(clock=clock))
        await cache.set("k", "v", 5)
        clock.advance(6)
        assert await cache.get("k") is None

    async def test_increment_is_atomic_and_sets_ttl_only_on_creation(self):
        clock = FakeClock()
        client = FakeRedisClient(clock=clock)
        cache = build_cache(client)

        assert await cache.increment("c", 1, ttl_seconds=10) == 1
        assert await cache.increment("c", 1, ttl_seconds=10) == 2
        assert await cache.increment("c", 5, ttl_seconds=10) == 7

        # TTL was set once, at creation — a busy counter must not become
        # immortal by having its TTL refreshed on every increment.
        clock.advance(11)
        assert await cache.get("c") is None

    async def test_oversized_values_are_skipped_not_stored(self, fake_redis_client):
        cache = build_cache(fake_redis_client, CACHE_MAX_VALUE_BYTES=200)
        assert await cache.set("big", {"payload": "x" * 5000}, 60) is False
        assert await cache.get("big") is None

    async def test_disabled_cache_is_a_total_no_op(self, fake_redis_client):
        cache = build_cache(fake_redis_client, CACHE_ENABLED=False)
        assert await cache.set("k", 1, 60) is False
        assert await cache.get("k") is None
        assert await cache.delete("k") == 0
        assert fake_redis_client.command_counts == {}, "disabled cache must not touch Redis at all"

    async def test_scan_keys_finds_by_pattern(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        folder_id = uuid.uuid4()
        await cache.set(f"nimbusfs:folder:{folder_id}", 1, 60)
        await cache.set(f"nimbusfs:folder:{folder_id}:children:abc", 2, 60)
        await cache.set(f"nimbusfs:file:{uuid.uuid4()}", 3, 60)

        found = await cache.scan_keys(f"nimbusfs:folder:{folder_id}*")
        assert len(found) == 2


# =====================================================================
# 4. Cache-aside + stampede protection
# =====================================================================
class TestCacheAsideAndStampede:
    async def test_get_or_set_populates_then_serves_from_cache(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return {"name": "Projects"}

        assert await cache.get_or_set("k", loader, 60) == {"name": "Projects"}
        assert await cache.get_or_set("k", loader, 60) == {"name": "Projects"}
        assert calls == 1, "second read must be served from cache"

    async def test_empty_list_result_is_cached_not_treated_as_a_miss(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return []

        assert await cache.get_or_set("k", loader, 60) == []
        assert await cache.get_or_set("k", loader, 60) == []
        assert calls == 1, "an empty listing is a legitimate cached value"

    async def test_stampede_protection_collapses_concurrent_misses(self, fake_redis_client):
        """
        50 concurrent requests for one uncached key must NOT produce 50
        database round trips. The guarantee is 'far fewer', not 'exactly
        one' — see CacheService.get_or_set for why an exactly-one
        guarantee would require unbounded blocking.
        """
        cache = build_cache(fake_redis_client)
        db_hits = 0

        async def slow_loader():
            nonlocal db_hits
            db_hits += 1
            await asyncio.sleep(0.02)  # stand-in for a real query
            return {"value": "loaded"}

        results = await asyncio.gather(*(cache.get_or_set("hot", slow_loader, 60) for _ in range(50)))

        assert all(r == {"value": "loaded"} for r in results), "every caller still gets a correct answer"
        assert db_hits >= 1
        assert db_hits < 10, f"stampede protection failed: {db_hits}/50 requests reached the source"

    async def test_followers_read_through_rather_than_blocking_forever(self, fake_redis_client):
        """
        A leader that never publishes must not hang the followers: they
        wait a bounded time and then hit the source themselves.
        """
        cache = build_cache(fake_redis_client, CACHE_STAMPEDE_WAIT_SECONDS=0.05)

        # Pre-take the stampede lock so no caller can become the leader.
        keys = CacheKeyBuilder(PREFIX)
        blocker = DistributedLock(fake_redis_client, keys.stampede_lock("hot"), 30)
        assert await blocker.acquire() is True

        async def loader():
            return {"value": "from_db"}

        result = await asyncio.wait_for(cache.get_or_set("hot", loader, 60), timeout=3)
        assert result == {"value": "from_db"}

    async def test_stampede_lock_is_released_even_when_the_loader_raises(self, fake_redis_client):
        cache = build_cache(fake_redis_client)

        async def failing_loader():
            raise RuntimeError("db exploded")

        with pytest.raises(RuntimeError):
            await cache.get_or_set("k", failing_loader, 60)

        # The lock must be free again, or the key would be un-populatable
        # until the lock's TTL expired.
        lock = DistributedLock(fake_redis_client, CacheKeyBuilder(PREFIX).stampede_lock("k"), 5)
        assert await lock.acquire() is True

    async def test_cacheable_predicate_can_veto_a_write(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return {"items": [1, 2, 3]}

        await cache.get_or_set("k", loader, 60, cacheable=lambda v: len(v["items"]) <= 2)
        await cache.get_or_set("k", loader, 60, cacheable=lambda v: len(v["items"]) <= 2)
        assert calls == 2, "a vetoed result must not be cached"


# =====================================================================
# 5. Redis failure -> graceful degradation
# =====================================================================
class TestRedisFailureDegradation:
    async def test_get_degrades_to_a_miss(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        fake_redis_client.start_failing()
        assert await cache.get("k") is None
        assert cache.errors == 1, "the failure must be counted and logged, not silently ignored"

    async def test_set_degrades_to_a_skipped_write(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        fake_redis_client.start_failing()
        assert await cache.set("k", {"a": 1}, 60) is False
        assert cache.errors >= 1

    async def test_delete_and_exists_degrade_without_raising(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        fake_redis_client.start_failing()
        assert await cache.delete("k") == 0
        assert await cache.exists("k") is False
        assert await cache.increment("k") is None
        assert await cache.expire("k", 5) is False

    async def test_get_or_set_still_returns_the_real_answer(self, fake_redis_client):
        """The whole point: Postgres is authoritative, so the request succeeds."""
        cache = build_cache(fake_redis_client)
        fake_redis_client.start_failing()

        async def loader():
            return {"value": "from_postgres"}

        assert await cache.get_or_set("k", loader, 60) == {"value": "from_postgres"}

    async def test_redis_dying_mid_request_is_survivable(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        await cache.set("k", {"a": 1}, 60)
        fake_redis_client.start_failing(after=1)  # one more command works, then it dies

        async def loader():
            return {"a": 2}

        assert await cache.get_or_set("k", loader, 60) is not None

    async def test_invalidation_failure_does_not_raise(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        invalidator = CacheInvalidator(cache)
        fake_redis_client.start_failing()
        await invalidator.file_changed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())  # must not raise

    async def test_scan_failure_degrades_to_empty(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        fake_redis_client.start_failing()
        assert await cache.scan_keys("nimbusfs:*") == []


# =====================================================================
# 6. CacheInvalidator fan-out
# =====================================================================
class TestCacheInvalidator:
    async def test_folder_change_clears_folder_derived_and_parent_listing_keys(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        keys = cache.keys
        invalidator = CacheInvalidator(cache)

        owner, parent, folder = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await cache.set(keys.folder(folder), {"a": 1}, 60)
        await cache.set(keys.folder_children(folder, owner, {"s": 1}), [], 60)
        await cache.set(keys.folder_breadcrumbs(folder), [], 60)
        await cache.set(keys.folder_children(parent, owner, {"s": 1}), [], 60)
        survivor = keys.folder(uuid.uuid4())
        await cache.set(survivor, {"unrelated": True}, 60)

        await invalidator.folder_changed(folder, owner, parent)

        assert await cache.get(keys.folder(folder)) is None
        assert await cache.get(keys.folder_children(folder, owner, {"s": 1})) is None
        assert await cache.get(keys.folder_breadcrumbs(folder)) is None
        assert await cache.get(keys.folder_children(parent, owner, {"s": 1})) is None
        assert await cache.get(survivor) is not None, "invalidation must not be a flush-all"

    async def test_folder_move_clears_both_old_and_new_parent_listings(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        keys = cache.keys
        invalidator = CacheInvalidator(cache)
        owner, old_parent, new_parent, folder = (uuid.uuid4() for _ in range(4))

        await cache.set(keys.folder_children(old_parent, owner, {}), [], 60)
        await cache.set(keys.folder_children(new_parent, owner, {}), [], 60)

        await invalidator.folder_moved(folder, owner, old_parent, new_parent)

        assert await cache.get(keys.folder_children(old_parent, owner, {})) is None
        assert await cache.get(keys.folder_children(new_parent, owner, {})) is None

    async def test_file_change_clears_file_folder_listing_and_search(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        keys = cache.keys
        invalidator = CacheInvalidator(cache)
        owner, folder, file_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        await cache.set(keys.file(file_id), {"a": 1}, 60)
        await cache.set(keys.file_versions(file_id), [], 60)
        await cache.set(keys.folder_children(folder, owner, {}), [], 60)
        await cache.set(keys.search(owner, {"q": "report"}), {"items": []}, 60)

        await invalidator.file_changed(file_id, owner, folder)

        assert await cache.get(keys.file(file_id)) is None
        assert await cache.get(keys.file_versions(file_id)) is None
        assert await cache.get(keys.folder_children(folder, owner, {})) is None
        assert await cache.get(keys.search(owner, {"q": "report"})) is None

    async def test_search_invalidation_is_scoped_to_one_user(self, fake_redis_client):
        cache = build_cache(fake_redis_client)
        keys = cache.keys
        invalidator = CacheInvalidator(cache)
        alice, bob = uuid.uuid4(), uuid.uuid4()

        await cache.set(keys.search(alice, {"q": "x"}), {"items": []}, 60)
        await cache.set(keys.search(bob, {"q": "x"}), {"items": []}, 60)

        await invalidator.search_changed(alice)

        assert await cache.get(keys.search(alice, {"q": "x"})) is None
        assert await cache.get(keys.search(bob, {"q": "x"})) is not None


# =====================================================================
# 6b. Post-invalidation write guard (opt-in)
# =====================================================================
class TestWriteGuard:
    async def test_guard_suppresses_repopulation_for_its_window(self):
        clock = FakeClock()
        client = FakeRedisClient(clock=clock)
        cache = build_cache(client, CACHE_WRITE_GUARD_SECONDS=2.0)

        await cache.set("k", {"v": "old"}, 60)
        await cache.invalidate("k")

        # A concurrent reader that races the invalidation must not be able
        # to write pre-commit data back in.
        assert await cache.set("k", {"v": "stale"}, 60) is False
        assert await cache.get("k") is None

        clock.advance(3)
        assert await cache.set("k", {"v": "fresh"}, 60) is True

    async def test_guard_is_on_by_default(self, fake_redis_client):
        # Default is CACHE_WRITE_GUARD_SECONDS=1.5 (flipped ON in a Phase 7
        # follow-up — see CONTEXT.md and docs/PHASE_7_REDIS_DESIGN.md §7
        # Race #1): a write immediately after invalidation is guarded off.
        cache = build_cache(fake_redis_client)
        await cache.set("k", {"v": 1}, 60)
        await cache.invalidate("k")
        assert await cache.set("k", {"v": 2}, 60) is False

    async def test_guard_can_be_disabled(self, fake_redis_client):
        cache = build_cache(fake_redis_client, CACHE_WRITE_GUARD_SECONDS=0.0)
        await cache.set("k", {"v": 1}, 60)
        await cache.invalidate("k")
        assert await cache.set("k", {"v": 2}, 60) is True


# =====================================================================
# 7. Distributed locks
# =====================================================================
class TestDistributedLock:
    async def test_acquire_succeeds_then_blocks_a_second_holder(self, fake_redis_client):
        first = DistributedLock(fake_redis_client, "resource", 30)
        second = DistributedLock(fake_redis_client, "resource", 30)
        assert await first.acquire() is True
        assert await second.acquire() is False, "two holders of one lock is the bug this prevents"

    async def test_release_frees_the_lock_for_the_next_caller(self, fake_redis_client):
        first = DistributedLock(fake_redis_client, "resource", 30)
        await first.acquire()
        assert await first.release() is True
        assert await DistributedLock(fake_redis_client, "resource", 30).acquire() is True

    async def test_a_lock_expires_on_its_own_when_its_holder_never_releases(self):
        """Models a crashed pod: the TTL is what stops it blocking everyone forever."""
        clock = FakeClock()
        client = FakeRedisClient(clock=clock)
        crashed = DistributedLock(client, "resource", 10)
        assert await crashed.acquire() is True

        clock.advance(11)
        assert await DistributedLock(client, "resource", 10).acquire() is True

    async def test_release_never_deletes_a_lock_someone_else_now_holds(self):
        """The classic 'lost lock' bug, guarded by the Lua token check."""
        clock = FakeClock()
        client = FakeRedisClient(clock=clock)

        slow = DistributedLock(client, "resource", 10)
        await slow.acquire()
        clock.advance(11)  # slow's lock silently expired

        fast = DistributedLock(client, "resource", 10)
        assert await fast.acquire() is True

        assert await slow.release() is False, "must not delete the new holder's lock"
        assert await fast.owns() is True

    async def test_strict_release_raises_when_ownership_was_lost(self):
        clock = FakeClock()
        client = FakeRedisClient(clock=clock)
        lock = DistributedLock(client, "resource", 10)
        await lock.acquire()
        clock.advance(11)
        await DistributedLock(client, "resource", 10).acquire()

        with pytest.raises(LockOwnershipError):
            await lock.release(strict=True)

    async def test_owns_reports_authoritative_ownership(self, fake_redis_client):
        lock = DistributedLock(fake_redis_client, "resource", 30)
        assert await lock.owns() is False
        await lock.acquire()
        assert lock.is_held is True
        assert await lock.owns() is True
        await lock.release()
        assert await lock.owns() is False

    async def test_extend_only_works_while_still_owned(self):
        clock = FakeClock()
        client = FakeRedisClient(clock=clock)
        lock = DistributedLock(client, "resource", 10)
        await lock.acquire()
        assert await lock.extend(60) is True
        clock.advance(11)
        assert await DistributedLock(client, "resource", 10).acquire() is False, "extension must have held"

    async def test_acquire_with_timeout_gives_up_and_raises(self, fake_redis_client):
        holder = DistributedLock(fake_redis_client, "resource", 30)
        await holder.acquire()

        waiter = DistributedLock(fake_redis_client, "resource", 30)
        with pytest.raises(LockAcquisitionTimeout):
            await waiter.acquire_with_timeout(0.05, 0.01)

    async def test_acquire_with_timeout_can_return_false_instead_of_raising(self, fake_redis_client):
        await DistributedLock(fake_redis_client, "resource", 30).acquire()
        waiter = DistributedLock(fake_redis_client, "resource", 30)
        assert await waiter.acquire_with_timeout(0.05, 0.01, raise_on_timeout=False) is False

    async def test_acquire_with_timeout_succeeds_once_the_holder_releases(self, fake_redis_client):
        holder = DistributedLock(fake_redis_client, "resource", 30)
        await holder.acquire()

        async def release_soon():
            await asyncio.sleep(0.05)
            await holder.release()

        waiter = DistributedLock(fake_redis_client, "resource", 30)
        _, acquired = await asyncio.gather(release_soon(), waiter.acquire_with_timeout(2.0, 0.01))
        assert acquired is True

    async def test_only_one_of_many_concurrent_contenders_wins(self, fake_redis_client):
        locks = [DistributedLock(fake_redis_client, "resource", 30) for _ in range(20)]
        results = await asyncio.gather(*(lock.acquire() for lock in locks))
        assert sum(results) == 1


class TestDistributedLockService:
    def _service(self, client) -> DistributedLockService:
        return DistributedLockService(
            DistributedLockFactory(client, default_ttl_seconds=30),
            default_timeout_seconds=0.05,
            retry_interval_seconds=0.01,
        )

    async def test_guard_acquires_and_always_releases(self, fake_redis_client):
        service = self._service(fake_redis_client)
        async with service.guard("resource") as lock:
            assert await lock.owns() is True
        assert await DistributedLock(fake_redis_client, "resource", 30).acquire() is True

    async def test_guard_releases_on_exception(self, fake_redis_client):
        service = self._service(fake_redis_client)
        with pytest.raises(ValueError):
            async with service.guard("resource"):
                raise ValueError("boom")
        assert await DistributedLock(fake_redis_client, "resource", 30).acquire() is True

    async def test_contention_surfaces_as_a_timeout_not_a_hang(self, fake_redis_client):
        await DistributedLock(fake_redis_client, "resource", 30).acquire()
        service = self._service(fake_redis_client)
        with pytest.raises(LockAcquisitionTimeout):
            async with service.guard("resource"):
                pass

    async def test_redis_failure_at_acquire_is_never_mistaken_for_a_free_lock(self, fake_redis_client):
        """
        The most dangerous possible bug in a lock: treating 'I could not
        reach the coordinator' as 'nobody holds it'.
        """
        service = self._service(fake_redis_client)
        fake_redis_client.start_failing()
        with pytest.raises(DistributedLockError):
            async with service.guard("resource"):
                pass

    async def test_redis_failure_at_release_does_not_fail_the_operation(self, fake_redis_client):
        service = self._service(fake_redis_client)
        lock = await service.acquire("resource")
        fake_redis_client.start_failing()
        assert await service.release(lock) is False  # logged, not raised


# =====================================================================
# 8. End-to-end cache/DB consistency through the real API
# =====================================================================
def _folder_key(folder_id: str) -> str:
    return f"nimbusfs:folder:{folder_id}"


def _breadcrumb_key(folder_id: str) -> str:
    return f"nimbusfs:folder:{folder_id}:breadcrumbs"


class TestEndToEndCacheConsistency:
    async def test_folder_read_populates_the_cache_and_repeats_are_served_from_it(
        self, authed_client: AsyncClient, fake_redis_client: FakeRedisClient
    ):
        created = await authed_client.post("/api/v1/folders", json={"name": "Projects"})
        folder_id = created.json()["data"]["id"]

        first = await authed_client.get(f"/api/v1/folders/{folder_id}")
        assert first.status_code == 200
        assert _folder_key(folder_id) in fake_redis_client._store

        second = await authed_client.get(f"/api/v1/folders/{folder_id}")
        assert second.json()["data"]["name"] == "Projects"

    async def test_rename_invalidates_so_the_next_read_is_never_stale(
        self, authed_client: AsyncClient, fake_redis_client: FakeRedisClient
    ):
        created = await authed_client.post("/api/v1/folders", json={"name": "Old"})
        folder_id = created.json()["data"]["id"]

        await authed_client.get(f"/api/v1/folders/{folder_id}")  # warm the cache
        assert _folder_key(folder_id) in fake_redis_client._store

        await authed_client.put(f"/api/v1/folders/{folder_id}", json={"name": "New"})
        assert _folder_key(folder_id) not in fake_redis_client._store, "rename must invalidate"

        after = await authed_client.get(f"/api/v1/folders/{folder_id}")
        assert after.json()["data"]["name"] == "New"

    async def test_creating_a_child_invalidates_the_parent_listing(self, authed_client: AsyncClient):
        parent = await authed_client.post("/api/v1/folders", json={"name": "Parent"})
        parent_id = parent.json()["data"]["id"]

        first = await authed_client.get("/api/v1/folders", params={"parent_folder_id": parent_id})
        assert first.json()["data"] == []

        await authed_client.post(
            "/api/v1/folders", json={"name": "Child", "parent_folder_id": parent_id}
        )

        second = await authed_client.get("/api/v1/folders", params={"parent_folder_id": parent_id})
        assert [f["name"] for f in second.json()["data"]] == ["Child"]

    async def test_move_invalidates_both_source_and_destination_listings(self, authed_client: AsyncClient):
        a = (await authed_client.post("/api/v1/folders", json={"name": "A"})).json()["data"]["id"]
        b = (await authed_client.post("/api/v1/folders", json={"name": "B"})).json()["data"]["id"]
        child = (
            await authed_client.post("/api/v1/folders", json={"name": "Child", "parent_folder_id": a})
        ).json()["data"]["id"]

        await authed_client.get("/api/v1/folders", params={"parent_folder_id": a})
        await authed_client.get("/api/v1/folders", params={"parent_folder_id": b})

        await authed_client.post(f"/api/v1/folders/{child}/move", json={"new_parent_folder_id": b})

        in_a = await authed_client.get("/api/v1/folders", params={"parent_folder_id": a})
        in_b = await authed_client.get("/api/v1/folders", params={"parent_folder_id": b})
        assert in_a.json()["data"] == []
        assert [f["name"] for f in in_b.json()["data"]] == ["Child"]

    async def test_trash_then_restore_round_trips_without_serving_a_stale_folder(
        self, authed_client: AsyncClient
    ):
        folder_id = (
            await authed_client.post("/api/v1/folders", json={"name": "Temp"})
        ).json()["data"]["id"]

        await authed_client.get(f"/api/v1/folders/{folder_id}")  # warm
        await authed_client.delete(f"/api/v1/folders/{folder_id}")

        gone = await authed_client.get(f"/api/v1/folders/{folder_id}")
        assert gone.status_code == 404, "a trashed folder must not keep resolving from cache"

        await authed_client.post(f"/api/v1/folders/{folder_id}/restore")
        back = await authed_client.get(f"/api/v1/folders/{folder_id}")
        assert back.status_code == 200

    async def test_breadcrumbs_are_cached_and_invalidated_on_rename(self, authed_client: AsyncClient):
        parent = (await authed_client.post("/api/v1/folders", json={"name": "Root"})).json()["data"]["id"]
        child = (
            await authed_client.post("/api/v1/folders", json={"name": "Leaf", "parent_folder_id": parent})
        ).json()["data"]["id"]

        first = await authed_client.get("/api/v1/folders/breadcrumb", params={"folder_id": child})
        assert [i["name"] for i in first.json()["data"]["items"]] == ["Root", "Leaf"]

        await authed_client.put(f"/api/v1/folders/{child}", json={"name": "Leaf2"})

        after = await authed_client.get("/api/v1/folders/breadcrumb", params={"folder_id": child})
        assert [i["name"] for i in after.json()["data"]["items"]] == ["Root", "Leaf2"]

    async def test_renaming_an_ancestor_precisely_invalidates_a_descendants_breadcrumb(
        self, authed_client: AsyncClient, fake_redis_client: FakeRedisClient
    ):
        """
        Regression test for the fixed "descendant fan-out" gap (see
        CacheInvalidator.descendant_breadcrumbs_changed): renaming an
        ANCESTOR two levels up must update a deep descendant's breadcrumb
        immediately, not leave it stale until TTL.
        """
        root = (await authed_client.post("/api/v1/folders", json={"name": "Root"})).json()["data"]["id"]
        mid = (
            await authed_client.post("/api/v1/folders", json={"name": "Mid", "parent_folder_id": root})
        ).json()["data"]["id"]
        leaf = (
            await authed_client.post("/api/v1/folders", json={"name": "Leaf", "parent_folder_id": mid})
        ).json()["data"]["id"]

        first = await authed_client.get("/api/v1/folders/breadcrumb", params={"folder_id": leaf})
        assert [i["name"] for i in first.json()["data"]["items"]] == ["Root", "Mid", "Leaf"]
        assert _breadcrumb_key(leaf) in fake_redis_client._store

        # Rename the ROOT ancestor, not the leaf itself.
        await authed_client.put(f"/api/v1/folders/{root}", json={"name": "Root2"})

        # The precise fix: the leaf's own breadcrumb cache entry is gone
        # immediately, not merely correct-when-eventually-recomputed.
        assert _breadcrumb_key(leaf) not in fake_redis_client._store, (
            "an ancestor rename must precisely invalidate every descendant's "
            "breadcrumb cache, not leave it to TTL"
        )

        after = await authed_client.get("/api/v1/folders/breadcrumb", params={"folder_id": leaf})
        assert [i["name"] for i in after.json()["data"]["items"]] == ["Root2", "Mid", "Leaf"]

    async def test_file_metadata_read_update_and_delete_stay_consistent(self, authed_client: AsyncClient):
        created = await authed_client.post(
            "/api/v1/metadata",
            json={"original_filename": "report.pdf", "mime_type": "application/pdf", "size": 100},
        )
        file_id = created.json()["data"]["id"]

        assert (await authed_client.get(f"/api/v1/metadata/{file_id}")).json()["data"][
            "original_filename"
        ] == "report.pdf"

        await authed_client.post(f"/api/v1/metadata/{file_id}/rename", json={"name": "final.pdf"})
        assert (await authed_client.get(f"/api/v1/metadata/{file_id}")).json()["data"][
            "original_filename"
        ] == "final.pdf"

        await authed_client.delete(f"/api/v1/metadata/{file_id}")
        assert (await authed_client.get(f"/api/v1/metadata/{file_id}")).status_code == 404

        await authed_client.post(f"/api/v1/metadata/{file_id}/restore")
        assert (await authed_client.get(f"/api/v1/metadata/{file_id}")).status_code == 200

    async def test_file_versions_cache_reflects_a_new_version(self, authed_client: AsyncClient):
        created = await authed_client.post(
            "/api/v1/metadata",
            json={"original_filename": "doc.txt", "mime_type": "text/plain", "size": 10},
        )
        file_id = created.json()["data"]["id"]

        first = await authed_client.get(f"/api/v1/metadata/{file_id}/versions")
        assert len(first.json()["data"]) == 1

        await authed_client.put(f"/api/v1/metadata/{file_id}", json={"size": 20, "checksum": "abc"})

        after = await authed_client.get(f"/api/v1/metadata/{file_id}/versions")
        assert len(after.json()["data"]) == 2, "version cache must be invalidated by a version-creating write"

    async def test_search_results_reflect_a_newly_created_file(self, authed_client: AsyncClient):
        first = await authed_client.get("/api/v1/metadata/search", params={"q": "invoice"})
        assert first.json()["data"]["total"] == 0

        await authed_client.post(
            "/api/v1/metadata",
            json={"original_filename": "invoice-2026.pdf", "mime_type": "application/pdf", "size": 42},
        )

        after = await authed_client.get("/api/v1/metadata/search", params={"q": "invoice"})
        assert after.json()["data"]["total"] == 1, "a file write must clear that owner's cached search pages"

    async def test_cached_reads_still_enforce_ownership(
        self, client: AsyncClient, valid_user_payload: dict
    ):
        """
        The security property that must survive caching: user B may not
        read user A's folder, and gets a 404 (never a 403) so folder IDs
        remain unguessable.
        """
        await client.post("/api/v1/auth/register", json=valid_user_payload)
        login_a = await client.post(
            "/api/v1/auth/login",
            data={"username": valid_user_payload["email"], "password": valid_user_payload["password"]},
        )
        token_a = login_a.json()["data"]["access_token"]

        created = await client.post(
            "/api/v1/folders", json={"name": "Private"}, headers={"Authorization": f"Bearer {token_a}"}
        )
        folder_id = created.json()["data"]["id"]

        # Warm the cache as the owner.
        assert (
            await client.get(
                f"/api/v1/folders/{folder_id}", headers={"Authorization": f"Bearer {token_a}"}
            )
        ).status_code == 200

        other = {**valid_user_payload, "email": "mallory@nimbusfs.io"}
        await client.post("/api/v1/auth/register", json=other)
        login_b = await client.post(
            "/api/v1/auth/login", data={"username": other["email"], "password": other["password"]}
        )
        token_b = login_b.json()["data"]["access_token"]

        stolen = await client.get(
            f"/api/v1/folders/{folder_id}", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert stolen.status_code == 404, "the cache must not become an authorization bypass"

    async def test_api_keeps_working_with_redis_completely_down(
        self, authed_client: AsyncClient, fake_redis_client: FakeRedisClient
    ):
        """Postgres is authoritative — a dead cache must be invisible to users."""
        created = await authed_client.post("/api/v1/folders", json={"name": "Resilient"})
        folder_id = created.json()["data"]["id"]

        fake_redis_client.start_failing()

        read = await authed_client.get(f"/api/v1/folders/{folder_id}")
        assert read.status_code == 200
        assert read.json()["data"]["name"] == "Resilient"

        renamed = await authed_client.put(f"/api/v1/folders/{folder_id}", json={"name": "Still Fine"})
        assert renamed.status_code == 200

        listing = await authed_client.get("/api/v1/folders")
        assert listing.status_code == 200
