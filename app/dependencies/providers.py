"""
Dependency injection providers.

Design decision: FastAPI's `Depends` graph IS our DI container. Each
provider function declares its own dependencies (e.g. `get_user_service`
depends on `get_db`), and FastAPI resolves the whole chain per-request.
This keeps constructors simple and explicit, and makes it trivial to
override any provider in tests via `app.dependency_overrides`.
"""

from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends
from google.cloud import storage
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache.keys import CacheKeyBuilder
from app.core.cache.policy import CachePolicy
from app.core.config import get_settings
from app.core.distributed_lock import DistributedLockFactory, DistributedLockService
from app.core.rate_limiter import RateLimiter
from app.database.gcs import get_storage_client
from app.database.redis import get_redis
from app.database.session import get_db
from app.dependencies.rate_limit import RateLimiterDep, get_rate_limiter
from app.events.publisher import EventPublisher
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.repositories.file_version_repository import FileVersionRepository
from app.repositories.folder_repository import FolderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.processed_event_repository import ProcessedEventRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.upload_chunk_repository import UploadChunkRepository
from app.repositories.upload_session_repository import UploadSessionRepository
from app.repositories.user_repository import UserRepository
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.cache_invalidator import CacheInvalidator
from app.services.cache_service import CacheService
from app.services.chunked_upload_service import ChunkedUploadService
from app.services.file_upload_service import FileUploadService
from app.services.file_validation_service import FileValidationService
from app.services.folder_service import FolderService
from app.services.idempotency_service import IdempotencyService
from app.services.metadata_service import MetadataService
from app.services.search_service import SearchService
from app.services.storage_service import StorageService
from app.services.trash_service import TrashService
from app.services.user_service import UserService
from app.services.version_service import VersionService

DbSession = Annotated[AsyncSession, Depends(get_db)]
RedisClientDep = Annotated[redis.Redis, Depends(get_redis)]


def get_distributed_lock_factory(client: RedisClientDep) -> DistributedLockFactory:
    settings = get_settings()
    return DistributedLockFactory(client, default_ttl_seconds=settings.LOCK_DEFAULT_TTL_SECONDS)


def get_idempotency_service(client: RedisClientDep) -> IdempotencyService:
    settings = get_settings()
    return IdempotencyService(client, ttl_seconds=settings.IDEMPOTENCY_KEY_TTL_SECONDS)


DistributedLockFactoryDep = Annotated[DistributedLockFactory, Depends(get_distributed_lock_factory)]
IdempotencyServiceDep = Annotated[IdempotencyService, Depends(get_idempotency_service)]


def get_distributed_lock_service(factory: DistributedLockFactoryDep) -> DistributedLockService:
    """Phase 7: the bounded-wait/observable facade over the Phase 4 lock factory."""
    settings = get_settings()
    return DistributedLockService(
        factory,
        default_timeout_seconds=settings.LOCK_ACQUIRE_TIMEOUT_SECONDS,
        retry_interval_seconds=settings.LOCK_RETRY_INTERVAL_SECONDS,
    )


DistributedLockServiceDep = Annotated[DistributedLockService, Depends(get_distributed_lock_service)]


# ---------------------------------------------------------------------
# Caching (Phase 7)
# ---------------------------------------------------------------------
def get_cache_key_builder() -> CacheKeyBuilder:
    return CacheKeyBuilder(get_settings().CACHE_KEY_PREFIX)


def get_cache_policy() -> CachePolicy:
    return CachePolicy(get_settings())


def get_cache_service(
    client: RedisClientDep,
    lock_factory: DistributedLockFactoryDep,
) -> CacheService:
    """
    One `CacheService` per request.

    Request-scoped rather than process-scoped on purpose: it carries
    per-request hit/miss counters, and it binds the same Redis client the
    rest of the request uses — which is what lets `tests/conftest.py`
    swap in `FakeRedisClient` through the single existing `get_redis`
    override rather than needing a second, cache-specific override.
    """
    settings = get_settings()
    return CacheService(
        client,
        CachePolicy(settings),
        CacheKeyBuilder(settings.CACHE_KEY_PREFIX),
        enabled=settings.CACHE_ENABLED,
        lock_factory=lock_factory,
    )


CacheServiceDep = Annotated[CacheService, Depends(get_cache_service)]


def get_cache_invalidator(cache: CacheServiceDep) -> CacheInvalidator:
    return CacheInvalidator(cache)


CacheInvalidatorDep = Annotated[CacheInvalidator, Depends(get_cache_invalidator)]

# Rate limiting (Phase 7). `RateLimiterDep`/`get_rate_limiter` are defined
# in `app/dependencies/rate_limit.py` — the `rate_limit(...)` dependency
# factory there needs the limiter, and route modules import both, so
# defining the provider there keeps the import graph acyclic. They are
# re-exported under these names so every DI symbol stays discoverable from
# this one module, matching the convention used above.
RateLimiterProvider = get_rate_limiter
RateLimiterDependency = RateLimiterDep


def get_gcs_client() -> storage.Client:
    """
    Separate dependency (rather than calling `get_storage_client()`
    directly from `get_storage_service`) purely so tests can override
    just this one provider with a fake/mocked client via
    `app.dependency_overrides`, without needing to fake the entire
    `StorageService`.
    """
    return get_storage_client()


GCSClientDep = Annotated[storage.Client, Depends(get_gcs_client)]


def get_user_repository(session: DbSession) -> UserRepository:
    return UserRepository(session)


def get_refresh_token_repository(session: DbSession) -> RefreshTokenRepository:
    return RefreshTokenRepository(session)


def get_audit_log_repository(session: DbSession) -> AuditLogRepository:
    return AuditLogRepository(session)


AuditLogRepositoryDep = Annotated[AuditLogRepository, Depends(get_audit_log_repository)]


def get_audit_service(repository: AuditLogRepositoryDep) -> AuditService:
    """
    Phase 10: bound to the SAME request-scoped session as every other
    repository — same "one Unit of Work" mechanism the Phase 8 outbox
    provider's docstring explains. A security-audited mutation (e.g.
    `permanent_delete`) and its audit row therefore commit or roll back
    together, though `AuditService.record` itself still never raises
    (see its class docstring's degradation contract) — a failed audit
    *write* never fails the request even though it happens to share a
    transaction with one that would.
    """
    return AuditService(repository)


AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]


def get_folder_repository(session: DbSession) -> FolderRepository:
    return FolderRepository(session)


def get_file_metadata_repository(session: DbSession) -> FileMetadataRepository:
    return FileMetadataRepository(session)


def get_file_version_repository(session: DbSession) -> FileVersionRepository:
    return FileVersionRepository(session)


def get_upload_session_repository(session: DbSession) -> UploadSessionRepository:
    return UploadSessionRepository(session)


def get_upload_chunk_repository(session: DbSession) -> UploadChunkRepository:
    return UploadChunkRepository(session)


# ---------------------------------------------------------------------
# Event-driven architecture (Phase 8)
# ---------------------------------------------------------------------
def get_outbox_repository(session: DbSession) -> OutboxRepository:
    """
    Bound to the SAME request-scoped session as every other repository —
    which is the entire mechanism behind the transactional outbox.

    Because `get_db` owns the only `session.commit()` in the codebase,
    an outbox row inserted by a service during a request commits in the
    same transaction as the business row it describes. No new
    transaction-management code exists anywhere in Phase 8; the existing
    Unit of Work already provides the atomicity, and the outbox pattern
    is the technique for exploiting it.
    """
    return OutboxRepository(session)


def get_processed_event_repository(session: DbSession) -> ProcessedEventRepository:
    return ProcessedEventRepository(session)


def get_event_publisher() -> EventPublisher:
    """
    Process-level publisher, resolved per request via DI.

    Kept as its own provider (rather than being constructed inside a
    service) for the same reason `get_gcs_client` is: it is the seam a
    test overrides to inject `FakePublisherClient` without faking any
    business logic. Note the API itself does NOT publish directly —
    services write to the outbox and the outbox worker publishes. This
    provider exists for worker-side use and for any future endpoint that
    legitimately needs a direct publish.
    """
    return EventPublisher()


OutboxRepositoryDep = Annotated[OutboxRepository, Depends(get_outbox_repository)]
ProcessedEventRepositoryDep = Annotated[ProcessedEventRepository, Depends(get_processed_event_repository)]
EventPublisherDep = Annotated[EventPublisher, Depends(get_event_publisher)]


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]
RefreshTokenRepositoryDep = Annotated[RefreshTokenRepository, Depends(get_refresh_token_repository)]
FolderRepositoryDep = Annotated[FolderRepository, Depends(get_folder_repository)]
FileMetadataRepositoryDep = Annotated[FileMetadataRepository, Depends(get_file_metadata_repository)]
FileVersionRepositoryDep = Annotated[FileVersionRepository, Depends(get_file_version_repository)]
UploadSessionRepositoryDep = Annotated[UploadSessionRepository, Depends(get_upload_session_repository)]
UploadChunkRepositoryDep = Annotated[UploadChunkRepository, Depends(get_upload_chunk_repository)]


def get_auth_service(
    user_repository: UserRepositoryDep,
    refresh_token_repository: RefreshTokenRepositoryDep,
    audit: AuditServiceDep,
) -> AuthService:
    return AuthService(user_repository, refresh_token_repository, audit=audit)


def get_user_service(
    user_repository: UserRepositoryDep,
    cache: CacheServiceDep,
    invalidator: CacheInvalidatorDep,
) -> UserService:
    return UserService(user_repository, cache=cache, invalidator=invalidator)


def get_folder_service(
    folder_repository: FolderRepositoryDep,
    cache: CacheServiceDep,
    invalidator: CacheInvalidatorDep,
    outbox: OutboxRepositoryDep,
) -> FolderService:
    return FolderService(folder_repository, cache=cache, invalidator=invalidator, outbox=outbox)


def get_metadata_service(
    file_repository: FileMetadataRepositoryDep,
    version_repository: FileVersionRepositoryDep,
    folder_repository: FolderRepositoryDep,
    cache: CacheServiceDep,
    invalidator: CacheInvalidatorDep,
    outbox: OutboxRepositoryDep,
) -> MetadataService:
    return MetadataService(
        file_repository,
        version_repository,
        folder_repository,
        cache=cache,
        invalidator=invalidator,
        outbox=outbox,
    )


def get_search_service(file_repository: FileMetadataRepositoryDep, cache: CacheServiceDep) -> SearchService:
    return SearchService(file_repository, cache=cache)


def get_trash_service(
    folder_repository: FolderRepositoryDep, file_repository: FileMetadataRepositoryDep
) -> TrashService:
    return TrashService(folder_repository, file_repository)


def get_version_service(
    version_repository: FileVersionRepositoryDep,
    file_repository: FileMetadataRepositoryDep,
    cache: CacheServiceDep,
    invalidator: CacheInvalidatorDep,
) -> VersionService:
    return VersionService(version_repository, file_repository, cache=cache, invalidator=invalidator)


def get_storage_service(client: GCSClientDep) -> StorageService:
    return StorageService(client)


def get_file_validation_service() -> FileValidationService:
    return FileValidationService(get_settings())


StorageServiceDep = Annotated[StorageService, Depends(get_storage_service)]
FileValidationServiceDep = Annotated[FileValidationService, Depends(get_file_validation_service)]


def get_file_upload_service(
    file_repository: FileMetadataRepositoryDep,
    folder_repository: FolderRepositoryDep,
    version_repository: FileVersionRepositoryDep,
    storage_service: StorageServiceDep,
    validator: FileValidationServiceDep,
    invalidator: CacheInvalidatorDep,
    outbox: OutboxRepositoryDep,
    audit: AuditServiceDep,
) -> FileUploadService:
    return FileUploadService(
        file_repository, folder_repository, version_repository, storage_service, validator,
        invalidator=invalidator,
        outbox=outbox,
        audit=audit,
    )


FileUploadServiceDep = Annotated[FileUploadService, Depends(get_file_upload_service)]


def get_chunked_upload_service(
    upload_session_repository: UploadSessionRepositoryDep,
    upload_chunk_repository: UploadChunkRepositoryDep,
    file_repository: FileMetadataRepositoryDep,
    folder_repository: FolderRepositoryDep,
    version_repository: FileVersionRepositoryDep,
    storage_service: StorageServiceDep,
    validator: FileValidationServiceDep,
    lock_factory: DistributedLockFactoryDep,
    invalidator: CacheInvalidatorDep,
    outbox: OutboxRepositoryDep,
) -> ChunkedUploadService:
    return ChunkedUploadService(
        upload_session_repository,
        upload_chunk_repository,
        file_repository,
        folder_repository,
        version_repository,
        storage_service,
        validator,
        lock_factory,
        invalidator,
        outbox=outbox,
    )


ChunkedUploadServiceDep = Annotated[ChunkedUploadService, Depends(get_chunked_upload_service)]


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
UserServiceDep = Annotated[UserService, Depends(get_user_service)]
FolderServiceDep = Annotated[FolderService, Depends(get_folder_service)]
MetadataServiceDep = Annotated[MetadataService, Depends(get_metadata_service)]
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
TrashServiceDep = Annotated[TrashService, Depends(get_trash_service)]
VersionServiceDep = Annotated[VersionService, Depends(get_version_service)]