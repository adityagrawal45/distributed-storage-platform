"""
Cache primitives (Phase 7).

Deliberately split three ways so each concern can be reasoned about (and
unit-tested) alone:

- `keys.py`       — WHAT a key is called (naming/collision safety)
- `serializer.py` — HOW a value is encoded on the wire (format/versioning)
- `policy.py`     — HOW LONG a value lives (TTL per entity)

The orchestration that uses all three lives in
`app/services/cache_service.py`; nothing outside that service (and
`app/services/cache_invalidator.py`) should talk to Redis for caching
purposes.
"""

from app.core.cache.keys import CacheKeyBuilder
from app.core.cache.policy import CacheEntity, CachePolicy
from app.core.cache.serializer import CACHE_SCHEMA_VERSION, CacheSerializer

__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CacheEntity",
    "CacheKeyBuilder",
    "CachePolicy",
    "CacheSerializer",
]
