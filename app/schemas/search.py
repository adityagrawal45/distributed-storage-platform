"""
Search/filter query-parameter bundles.

Design decision: mirrors `PaginationParams` — a plain class whose
`__init__` params are FastAPI `Query(...)` declarations, used via
`Depends()`. This keeps `GET /metadata/search` and `GET /folders`'s
query-string surface fully documented in Swagger (each param gets its
own description/type) while keeping the route handler signature a single
injected object instead of a dozen loose parameters.
"""

import uuid
from datetime import datetime

from fastapi import Query

from app.schemas.sorting import FileSortField, FolderSortField, SortOrder


class FileSearchParams:
    def __init__(
        self,
        q: str | None = Query(default=None, description="Search text matched against filename."),
        folder_id: uuid.UUID | None = Query(default=None, description="Restrict to a specific folder."),
        extension: str | None = Query(default=None, description="Filter by file extension, e.g. 'pdf'."),
        mime_type: str | None = Query(default=None, description="Filter by MIME type."),
        owner_id: uuid.UUID | None = Query(default=None, description="Filter by owner (admin use)."),
        version: int | None = Query(default=None, ge=1, description="Filter by current version number."),
        is_deleted: bool | None = Query(default=None, description="Filter by trash status."),
        created_after: datetime | None = Query(default=None),
        created_before: datetime | None = Query(default=None),
        updated_after: datetime | None = Query(default=None),
        updated_before: datetime | None = Query(default=None),
        sort_by: FileSortField = Query(default=FileSortField.CREATED_AT),
        sort_order: SortOrder = Query(default=SortOrder.DESC),
    ):
        self.q = q
        self.folder_id = folder_id
        self.extension = extension.lstrip(".").lower() if extension else None
        self.mime_type = mime_type
        self.owner_id = owner_id
        self.version = version
        self.is_deleted = is_deleted
        self.created_after = created_after
        self.created_before = created_before
        self.updated_after = updated_after
        self.updated_before = updated_before
        self.sort_by = sort_by
        self.sort_order = sort_order


class FolderListParams:
    def __init__(
        self,
        is_deleted: bool | None = Query(default=None, description="Filter by trash status."),
        sort_by: FolderSortField = Query(default=FolderSortField.NAME),
        sort_order: SortOrder = Query(default=SortOrder.ASC),
    ):
        self.is_deleted = is_deleted
        self.sort_by = sort_by
        self.sort_order = sort_order