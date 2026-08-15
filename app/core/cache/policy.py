"""
Per-entity cache TTL policy (Phase 7).

TTL is a correctness knob disguised as a performance knob: it is the hard
ceiling on how stale a read can be if invalidation is missed, dropped, or
raced. So the numbers are chosen per entity from "how bad is N seconds of
staleness for this thing, and how often does it actually change" — never
one global TTL, and never a literal at a call site.

    Entity              Default   Why
    ------------------  --------  ----------------------------------------
    user                15 min    Profile/role/status changes are rare and
                                  administrative. Note this cache is NOT on
                                  the auth path — `get_current_user` still
                                  re-reads the user row from Postgres on
                                  every request (Phase 1 decision: a
                                  deactivation must take effect
                                  immediately). Caching the *profile read*
                                  is safe; caching the *authorization
                                  decision* would not be.
    folder              5 min     Metadata mutates on rename/move/trash,
                                  all of which invalidate explicitly. TTL
                                  is the backstop, not the mechanism.
    folder children     5 min     Same, plus it changes whenever a child is
                                  created/deleted/moved — the highest-churn
                                  of the folder keys, so it also gets the
                                  most invalidation call sites.
    folder breadcrumbs  5 min     Changes only on a rename/move of an
                                  *ancestor*; see CacheInvalidator for the
                                  documented ancestor-fan-out simplification.
    file                5 min     Mirrors folder metadata.
    file versions       5 min     Append-only in practice; invalidated on
                                  any version-creating write.
    search              90 s      Deliberately the shortest. A search result
                                  set is a *derived* view over many rows,
                                  any one of which can change; exhaustively
                                  invalidating it is impossible to do
                                  precisely, so it leans on a short TTL plus
                                  coarse per-user invalidation.

All values are read from `Settings` (env-overridable) — see
`Settings.CACHE_TTL_*`. Nothing here hardcodes a duration.
"""

from __future__ import annotations

from enum import Enum

from app.core.config.settings import Settings


class CacheEntity(str, Enum):
    """The cacheable entity types. One TTL per member, no others allowed."""

    USER = "user"
    FOLDER = "folder"
    FOLDER_CHILDREN = "folder_children"
    FOLDER_BREADCRUMBS = "folder_breadcrumbs"
    FILE = "file"
    FILE_VERSIONS = "file_versions"
    SEARCH = "search"


class CachePolicy:
    """
    Resolves an entity type to its configured TTL.

    A tiny class rather than a dict constant so it can be constructed from
    `Settings` (and therefore overridden per environment / per test)
    instead of frozen at import time.
    """

    def __init__(self, settings: Settings):
        self._ttls: dict[CacheEntity, int] = {
            CacheEntity.USER: settings.CACHE_TTL_USER_SECONDS,
            CacheEntity.FOLDER: settings.CACHE_TTL_FOLDER_SECONDS,
            CacheEntity.FOLDER_CHILDREN: settings.CACHE_TTL_FOLDER_CHILDREN_SECONDS,
            CacheEntity.FOLDER_BREADCRUMBS: settings.CACHE_TTL_FOLDER_BREADCRUMBS_SECONDS,
            CacheEntity.FILE: settings.CACHE_TTL_FILE_SECONDS,
            CacheEntity.FILE_VERSIONS: settings.CACHE_TTL_FILE_VERSIONS_SECONDS,
            CacheEntity.SEARCH: settings.CACHE_TTL_SEARCH_SECONDS,
        }
        self.max_value_bytes = settings.CACHE_MAX_VALUE_BYTES
        self.search_max_items = settings.CACHE_SEARCH_MAX_ITEMS
        self.stampede_protection_enabled = settings.CACHE_STAMPEDE_PROTECTION_ENABLED
        self.stampede_lock_ttl_seconds = settings.CACHE_STAMPEDE_LOCK_TTL_SECONDS
        self.stampede_wait_seconds = settings.CACHE_STAMPEDE_WAIT_SECONDS
        self.stampede_poll_interval_seconds = settings.CACHE_STAMPEDE_POLL_INTERVAL_SECONDS
        self.write_guard_seconds = settings.CACHE_WRITE_GUARD_SECONDS

    def ttl_for(self, entity: CacheEntity) -> int:
        return self._ttls[entity]

    def as_dict(self) -> dict[str, int]:
        """Diagnostic view (used by logs/tests), not a public API surface."""
        return {entity.value: ttl for entity, ttl in self._ttls.items()}
