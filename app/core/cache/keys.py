"""
Centralized cache-key construction (Phase 7).

Why a builder instead of f-strings at call sites
------------------------------------------------
Cache keys are a *shared schema* between every replica of the app, and
between every version of the app that is running at the same time during
a rolling deploy. An f-string typo in one service silently creates a
second, permanently-cold key namespace; a subtly different key shape
between a reader and its invalidator creates a permanently-stale entry
that no amount of writing will ever clear. Both are the kind of bug that
only shows up in production, under load, intermittently. Centralizing
construction here means there is exactly one definition of every key, and
the invalidator and the reader cannot drift apart.

Collision safety
----------------
1. Every key starts with `Settings.CACHE_KEY_PREFIX` (default
   `nimbusfs`), so a shared Redis instance can be co-tenanted and
   `SCAN nimbusfs:*` is a complete inventory of what this app owns.
2. The second segment is always the *entity type*, so `folder:<uuid>` can
   never collide with `file:<uuid>` even if the two IDs were identical.
3. Variable-length, user-supplied, or structurally-complex components
   (search filters, listing sort parameters) are never interpolated raw —
   they are canonicalized to a sorted key/value string and SHA-256'd
   (`_fingerprint`). This makes keys fixed-length and bounded regardless
   of input, and removes any possibility of a value containing `:` and
   forging a different key shape.
4. `None` is encoded as the literal token `~none` rather than Python's
   `"None"`, so a folder literally named `None` and the root (no folder)
   cannot alias.

Key shapes owned by this module
-------------------------------
    nimbusfs:user:{user_id}
    nimbusfs:folder:{folder_id}
    nimbusfs:folder:{folder_id}:children:{params_fp}
    nimbusfs:folder:root:{owner_id}:children:{params_fp}
    nimbusfs:folder:{folder_id}:breadcrumbs
    nimbusfs:file:{file_id}
    nimbusfs:file:{file_id}:versions
    nimbusfs:search:{owner_id}:{query_fp}

Two documented deviations from the shapes named in the Phase 7 spec, both
deliberate:

- `...:children` carries a `params_fp` suffix. The children listing is
  parameterized (sort field, sort order, trash filter), and caching three
  different orderings under one key would serve a client the wrong
  ordering. The un-suffixed prefix is still what the invalidator deletes
  (via SCAN), so invalidation semantics are unchanged.
- `search:` is scoped by `owner_id` *before* the hash rather than folding
  the user into the hash alone. Two reasons: it makes "drop every cached
  search for this user" a single bounded SCAN pattern, and it guarantees
  that even a (theoretical) hash collision cannot leak one user's search
  results to another — the user segment is structural, not hashed.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

_NONE_TOKEN = "~none"


def _segment(value: Any) -> str:
    """Renders one key segment, with an unambiguous encoding for None."""
    if value is None:
        return _NONE_TOKEN
    return str(value)


def _fingerprint(parts: dict[str, Any]) -> str:
    """
    Canonical, fixed-length fingerprint of a set of query parameters.

    Sorted by key so that logically-identical parameter sets produce an
    identical fingerprint regardless of the order the caller happened to
    build the dict in — otherwise the same query would occupy two cache
    entries and neither would ever be hit reliably. Truncated to 32 hex
    chars (128 bits): still far beyond any realistic collision risk for a
    per-user keyspace, while keeping keys short enough to read in a Redis
    console during an incident.
    """
    canonical = "|".join(f"{key}={_segment(parts[key])}" for key in sorted(parts))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class CacheKeyBuilder:
    """
    Stateless-except-for-its-prefix key factory.

    Instantiated once per request via DI (it is trivially cheap), or used
    with the module-level default prefix in tests.
    """

    def __init__(self, prefix: str = "nimbusfs"):
        self._prefix = prefix.rstrip(":")

    # -- introspection -------------------------------------------------
    @property
    def prefix(self) -> str:
        return self._prefix

    def _key(self, *segments: Any) -> str:
        return ":".join([self._prefix, *(_segment(s) for s in segments)])

    # -- users ---------------------------------------------------------
    def user(self, user_id: uuid.UUID | str) -> str:
        return self._key("user", user_id)

    def user_prefix(self, user_id: uuid.UUID | str) -> str:
        """SCAN pattern matching every key owned by one user."""
        return self._key("user", user_id) + "*"

    # -- folders -------------------------------------------------------
    def folder(self, folder_id: uuid.UUID | str) -> str:
        return self._key("folder", folder_id)

    def folder_children(
        self,
        folder_id: uuid.UUID | str | None,
        owner_id: uuid.UUID | str,
        params: dict[str, Any] | None = None,
    ) -> str:
        fp = _fingerprint(params or {})
        if folder_id is None:
            # Root-level listing: there is no folder row to hang the key
            # off, so it is scoped by owner instead.
            return self._key("folder", "root", owner_id, "children", fp)
        return self._key("folder", folder_id, "children", fp)

    def folder_children_pattern(self, folder_id: uuid.UUID | str | None, owner_id: uuid.UUID | str) -> str:
        """SCAN pattern matching every parameter variant of one children listing."""
        if folder_id is None:
            return self._key("folder", "root", owner_id, "children") + ":*"
        return self._key("folder", folder_id, "children") + ":*"

    def folder_breadcrumbs(self, folder_id: uuid.UUID | str) -> str:
        return self._key("folder", folder_id, "breadcrumbs")

    def folder_pattern(self, folder_id: uuid.UUID | str) -> str:
        """SCAN pattern matching a folder's own key plus all of its derived keys."""
        return self._key("folder", folder_id) + "*"

    # -- files ---------------------------------------------------------
    def file(self, file_id: uuid.UUID | str) -> str:
        return self._key("file", file_id)

    def file_versions(self, file_id: uuid.UUID | str) -> str:
        return self._key("file", file_id, "versions")

    def file_pattern(self, file_id: uuid.UUID | str) -> str:
        return self._key("file", file_id) + "*"

    # -- search --------------------------------------------------------
    def search(self, owner_id: uuid.UUID | str, params: dict[str, Any]) -> str:
        return self._key("search", owner_id, _fingerprint(params))

    def search_pattern(self, owner_id: uuid.UUID | str) -> str:
        return self._key("search", owner_id) + ":*"

    # -- coordination --------------------------------------------------
    def stampede_lock(self, cache_key: str) -> str:
        """
        Lock key guarding population of `cache_key`.

        Hashes the target key rather than embedding it: cache keys are
        already namespaced and can be long, and `DistributedLock` adds its
        own `lock:` prefix on top.
        """
        return self._key("lock", "cache", hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:32])

    def write_guard(self, cache_key: str) -> str:
        """Tombstone key suppressing writes to `cache_key` after an invalidation."""
        return self._key("guard", hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:32])

    def rate_limit(self, category: str, identity: str) -> str:
        return self._key("ratelimit", category, identity)

    # -- observability -------------------------------------------------
    @staticmethod
    def redact(cache_key: str) -> str:
        """
        A stable, non-reversible label for a key, for log lines where the
        raw key could carry something sensitive.

        Search keys already hash their query text, but a future key shape
        might not; logging `redact(key)` keeps per-key log correlation
        possible (the same key always yields the same label) without ever
        putting user-supplied text into a log aggregator. Callers log the
        raw key for structural keys and this for anything user-derived —
        see `CacheService._log_key`.
        """
        return hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:16]
