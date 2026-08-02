"""Shared sort-field / sort-order enums for folder and file listing/search endpoints."""

from enum import Enum


class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"


class FolderSortField(str, Enum):
    NAME = "name"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"


class FileSortField(str, Enum):
    NAME = "name"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    SIZE = "size"
    TYPE = "type"