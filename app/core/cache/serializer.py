"""
Cache value serialization (Phase 7).

Why JSON and explicitly NOT pickle
----------------------------------
`pickle` is the obvious-looking choice — it round-trips arbitrary Python
objects with zero effort — and it is the wrong one here, for three
independent reasons, any one of which would be disqualifying:

1. **Unpickling is arbitrary code execution.** `pickle.loads` will
   happily construct objects and invoke `__reduce__` on whatever byte
   string it is handed. The cache is a *shared, network-reachable,
   multi-writer* datastore: every replica writes to it, it may be a
   managed service (Memorystore) reachable from anywhere inside the VPC,
   and in a co-tenanted deployment other workloads can write to it too.
   Treating anything read out of it as trusted input means a single Redis
   compromise (or a misconfigured NetworkPolicy, or one buggy sibling
   service writing to a colliding key) escalates directly to remote code
   execution inside every API pod. JSON parsing, by contrast, can at
   worst produce a wrong-shaped dict, which we reject.
2. **Pickle is not a stable cross-version format.** It encodes the
   fully-qualified class path of every object. Renaming or moving
   `app.schemas.folder.FolderRead` — a routine refactor — makes every
   cached entry written by the old build undecodable by the new one, and
   during a rolling deploy both builds are reading the same cache.
3. **Pickle is Python-only.** A cached entry should be inspectable with
   `redis-cli GET` during an incident, and readable by a future sidecar,
   debug tool, or non-Python service. JSON is.

The cost of JSON is that it has no native representation for `datetime`,
`UUID`, `Decimal`, `Enum`, or `set` — all of which appear in NimbusFS's
Pydantic v2 read schemas. That is handled by `_default` below (encode) and
by Pydantic's own coercion on the way back (decode): `FolderRead
.model_validate({...})` accepts ISO-8601 strings for `datetime` and hex
strings for `UUID` natively, so no bespoke decoder hook is needed. This
is deliberate — a decoder that tried to guess "is this string a
datetime?" would eventually mis-coerce a filename.

The envelope
------------
Values are never stored bare. Every entry is wrapped:

    {"v": <schema version>, "ts": <ISO-8601 write time>, "d": <payload>}

`v` exists so a future change to how payloads are shaped (a new envelope
field, a different datetime encoding, a compression scheme) does not
crash a running fleet mid-deploy. A reader that encounters an envelope
whose `v` it does not understand treats the entry as a **cache miss** —
it falls through to Postgres and repopulates — rather than raising. A
cache format change must never be able to take the app down; the worst it
may cost is one cold period. `ts` is purely diagnostic: it makes "how old
is this entry actually" answerable from `redis-cli` without correlating
against a TTL.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

# Bump this whenever the envelope shape or payload encoding changes in a
# way older builds cannot read. Old entries are then treated as misses.
CACHE_SCHEMA_VERSION = 1

_VERSION_FIELD = "v"
_TIMESTAMP_FIELD = "ts"
_PAYLOAD_FIELD = "d"


class CacheSerializer:
    """
    JSON envelope encoder/decoder. Stateless; safe to share.

    Neither method raises on bad *data* — `decode` returns `None` for
    anything it cannot make sense of (unparseable JSON, wrong envelope
    shape, unknown schema version), because every one of those cases is
    operationally identical to "the key was not there". `encode` DOES
    raise `CacheSerializationError` for a genuinely unencodable value,
    because that is a programming error in the caller worth surfacing —
    `CacheService` catches it, logs it, and skips the write.
    """

    @staticmethod
    def _default(value: Any) -> Any:
        """`json.dumps(default=...)` hook for the types our schemas use."""
        if isinstance(value, BaseModel):
            # mode="json" makes Pydantic do the nested coercion itself,
            # which is both faster and more faithful than doing it here.
            return value.model_dump(mode="json")
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, Decimal):
            # str(), not float() — float() silently loses precision, and
            # Pydantic accepts a decimal string back without complaint.
            return str(value)
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (set, frozenset)):
            return sorted(value, key=str)
        if isinstance(value, bytes):
            raise TypeError("Raw bytes are never cached by NimbusFS — file content belongs in GCS, not Redis.")
        raise TypeError(f"Object of type {type(value).__name__} is not cache-serializable.")

    @classmethod
    def encode(cls, value: Any) -> str:
        """Wraps `value` in the versioned envelope and returns JSON text."""
        from app.exceptions.custom_exceptions import CacheSerializationError

        envelope = {
            _VERSION_FIELD: CACHE_SCHEMA_VERSION,
            _TIMESTAMP_FIELD: datetime.now(timezone.utc).isoformat(),
            _PAYLOAD_FIELD: value,
        }
        try:
            # separators: no wasted whitespace — this goes over the wire
            # and into memory on every single write.
            return json.dumps(envelope, default=cls._default, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise CacheSerializationError(f"Could not encode value for cache: {exc}") from exc

    @classmethod
    def decode(cls, raw: str | bytes | None) -> tuple[bool, Any]:
        """
        Returns `(hit, payload)`.

        `hit` is False — never an exception — for a missing key, malformed
        JSON, a non-envelope value, or an envelope written by a schema
        version this build does not understand. Callers treat False
        exactly like "not in cache" and fall through to the database.
        """
        if raw is None:
            return False, None

        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return False, None

        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return False, None

        if not isinstance(envelope, dict):
            return False, None
        if envelope.get(_VERSION_FIELD) != CACHE_SCHEMA_VERSION:
            return False, None
        if _PAYLOAD_FIELD not in envelope:
            return False, None

        return True, envelope[_PAYLOAD_FIELD]

    @staticmethod
    def written_at(raw: str) -> datetime | None:
        """Diagnostic accessor for the envelope's write timestamp."""
        try:
            envelope = json.loads(raw)
            return datetime.fromisoformat(envelope[_TIMESTAMP_FIELD])
        except Exception:
            return None
