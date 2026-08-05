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

from app.core.config import get_settings
from app.core.distributed_lock import DistributedLockFactory
from app.database.gcs import get_storage_client
from app.database.redis import get_redis
from app.database.session import get_db
from app.repositories.file_metadata_repository import FileMetadataRepository
from app.repositories.file_version_repository import FileVersionRepository
from app.repositories.folder_repository import FolderRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
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


def get_folder_repository(session: DbSession) -> FolderRepository:
    return FolderRepository(session)


def get_file_metadata_repository(session: DbSession) -> FileMetadataRepository:
    return FileMetadataRepository(session)


def get_file_version_repository(session: DbSession) -> FileVersionRepository:
    return FileVersionRepository(session)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]
RefreshTokenRepositoryDep = Annotated[RefreshTokenRepository, Depends(get_refresh_token_repository)]
FolderRepositoryDep = Annotated[FolderRepository, Depends(get_folder_repository)]
FileMetadataRepositoryDep = Annotated[FileMetadataRepository, Depends(get_file_metadata_repository)]
FileVersionRepositoryDep = Annotated[FileVersionRepository, Depends(get_file_version_repository)]


def get_auth_service(
    user_repository: UserRepositoryDep,
    refresh_token_repository: RefreshTokenRepositoryDep,
) -> AuthService:
    return AuthService(user_repository, refresh_token_repository)


def get_user_service(user_repository: UserRepositoryDep) -> UserService:
    return UserService(user_repository)


def get_folder_service(folder_repository: FolderRepositoryDep) -> FolderService:
    return FolderService(folder_repository)


def get_metadata_service(
    file_repository: FileMetadataRepositoryDep,
    version_repository: FileVersionRepositoryDep,
    folder_repository: FolderRepositoryDep,
) -> MetadataService:
    return MetadataService(file_repository, version_repository, folder_repository)


def get_search_service(file_repository: FileMetadataRepositoryDep) -> SearchService:
    return SearchService(file_repository)


def get_trash_service(
    folder_repository: FolderRepositoryDep, file_repository: FileMetadataRepositoryDep
) -> TrashService:
    return TrashService(folder_repository, file_repository)


def get_version_service(
    version_repository: FileVersionRepositoryDep, file_repository: FileMetadataRepositoryDep
) -> VersionService:
    return VersionService(version_repository, file_repository)


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
) -> FileUploadService:
    return FileUploadService(file_repository, folder_repository, version_repository, storage_service, validator)


FileUploadServiceDep = Annotated[FileUploadService, Depends(get_file_upload_service)]


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
UserServiceDep = Annotated[UserService, Depends(get_user_service)]
FolderServiceDep = Annotated[FolderService, Depends(get_folder_service)]
MetadataServiceDep = Annotated[MetadataService, Depends(get_metadata_service)]
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
TrashServiceDep = Annotated[TrashService, Depends(get_trash_service)]
VersionServiceDep = Annotated[VersionService, Depends(get_version_service)]