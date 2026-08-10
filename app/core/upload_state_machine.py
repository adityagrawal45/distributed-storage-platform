"""
Upload session state machine (Phase 6).

Design decisions:
- Centralized here, not scattered across `ChunkedUploadService`'s
  methods or (worse) the API route handlers, per the phase requirement
  "do not scatter state transition logic throughout route handlers" —
  every place that changes an `UploadSession.status` calls
  `UploadStateMachine.assert_transition(current, target)` first, so the
  valid-transition graph has exactly one source of truth and can be
  unit-tested in complete isolation from the DB/HTTP/Redis layers this
  module has zero dependency on.
- A plain adjacency dict + two static methods, not a class hierarchy or
  a state-pattern object per status — the transition graph is small and
  static (7 states), and a dict makes "is X -> Y legal" a single lookup
  that's trivial to read, test, and diff in review.
- Terminal states (`COMPLETED`, `CANCELLED`, `EXPIRED`) map to an empty
  transition set — once there, `assert_transition` rejects every
  further transition attempt, which is exactly what "Do not allow
  COMPLETED -> CANCELLED" / "Do not allow EXPIRED -> COMPLETED" (Phase 6
  spec) requires, without needing special-cased checks for those two
  pairs specifically.
- `FAILED` is deliberately NOT terminal: a failed completion attempt
  (e.g. a transient GCS compose error, or a chunk that later gets
  re-uploaded) can transition back to `UPLOADING` to retry, or to
  `CANCELLED`/`EXPIRED` like any other non-terminal state. This is what
  lets `ChunkedUploadService.complete_upload` recover from a failure
  without forcing the client to start an entirely new upload session.
"""

from app.core.enums import UploadSessionStatus
from app.exceptions.custom_exceptions import InvalidUploadStateTransitionException

_VALID_TRANSITIONS: dict[UploadSessionStatus, frozenset[UploadSessionStatus]] = {
    UploadSessionStatus.INITIATED: frozenset(
        {UploadSessionStatus.UPLOADING, UploadSessionStatus.CANCELLED, UploadSessionStatus.EXPIRED}
    ),
    UploadSessionStatus.UPLOADING: frozenset(
        {
            UploadSessionStatus.COMPLETING,
            UploadSessionStatus.CANCELLED,
            UploadSessionStatus.EXPIRED,
            UploadSessionStatus.FAILED,
        }
    ),
    UploadSessionStatus.COMPLETING: frozenset({UploadSessionStatus.COMPLETED, UploadSessionStatus.FAILED}),
    UploadSessionStatus.FAILED: frozenset(
        {UploadSessionStatus.UPLOADING, UploadSessionStatus.CANCELLED, UploadSessionStatus.EXPIRED}
    ),
    UploadSessionStatus.COMPLETED: frozenset(),
    UploadSessionStatus.CANCELLED: frozenset(),
    UploadSessionStatus.EXPIRED: frozenset(),
}

TERMINAL_STATES: frozenset[UploadSessionStatus] = frozenset(
    {UploadSessionStatus.COMPLETED, UploadSessionStatus.CANCELLED, UploadSessionStatus.EXPIRED}
)


class UploadStateMachine:
    """Pure, stateless — operates only on `UploadSessionStatus` values passed in, never touches a session object."""

    @staticmethod
    def is_terminal(status: UploadSessionStatus) -> bool:
        return status in TERMINAL_STATES

    @staticmethod
    def can_transition(current: UploadSessionStatus, target: UploadSessionStatus) -> bool:
        return target in _VALID_TRANSITIONS.get(current, frozenset())

    @classmethod
    def assert_transition(cls, current: UploadSessionStatus, target: UploadSessionStatus) -> None:
        """Raises `InvalidUploadStateTransitionException` if `current -> target` isn't a legal edge."""
        if not cls.can_transition(current, target):
            raise InvalidUploadStateTransitionException(
                f"Cannot transition upload session from '{current.value}' to '{target.value}'."
            )
