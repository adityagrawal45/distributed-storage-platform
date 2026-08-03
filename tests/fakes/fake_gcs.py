"""
In-memory fake of the `google-cloud-storage` client surface that
`StorageService` actually calls. Deliberately NOT a `unittest.mock.Mock`:
a hand-written fake that actually stores bytes lets tests assert on real
round-trip behavior (upload then download returns the same bytes; delete
then download raises NotFound) rather than just "was this method called."
"""

from __future__ import annotations

from dataclasses import dataclass, field

from google.api_core import exceptions as gcs_exceptions


@dataclass
class _StoredObject:
    data: bytes
    content_type: str | None
    metadata: dict
    etag: str
    storage_class: str = "STANDARD"


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str):
        self._bucket = bucket
        self.name = name
        self.metadata: dict = {}
        self.etag: str | None = None
        self.size: int | None = None
        self.storage_class: str | None = None
        self.content_type: str | None = None

    def upload_from_file(self, stream, content_type=None, size=None) -> None:
        stream.seek(0)
        data = stream.read()
        etag = f"etag-{len(self._bucket.store)}-{self.name}"
        stored = _StoredObject(data=data, content_type=content_type, metadata=dict(self.metadata), etag=etag)
        self._bucket.store[self.name] = stored

    def reload(self) -> None:
        stored = self._bucket.store.get(self.name)
        if stored is None:
            raise gcs_exceptions.NotFound(f"{self.name} not found")
        self.size = len(stored.data)
        self.etag = stored.etag
        self.storage_class = stored.storage_class
        self.content_type = stored.content_type
        self.metadata = stored.metadata

    def exists(self) -> bool:
        return self.name in self._bucket.store

    def delete(self) -> None:
        if self.name not in self._bucket.store:
            raise gcs_exceptions.NotFound(f"{self.name} not found")
        del self._bucket.store[self.name]

    def download_as_bytes(self, start: int | None = None, end: int | None = None) -> bytes:
        stored = self._bucket.store.get(self.name)
        if stored is None:
            raise gcs_exceptions.NotFound(f"{self.name} not found")
        if start is None and end is None:
            return stored.data
        return stored.data[start : end + 1]

    def open(self, mode: str, chunk_size: int | None = None):
        stored = self._bucket.store.get(self.name)
        if stored is None:
            raise gcs_exceptions.NotFound(f"{self.name} not found")
        return _FakeReadHandle(stored.data)

    def generate_signed_url(self, version="v4", expiration=None, method="GET") -> str:
        if self.name not in self._bucket.store:
            raise gcs_exceptions.NotFound(f"{self.name} not found")
        return f"https://storage.fake.test/{self._bucket.name}/{self.name}?signature=fake&method={method}"


class _FakeReadHandle:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, size: int) -> bytes:
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@dataclass
class FakeBucket:
    name: str
    store: dict = field(default_factory=dict)

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)


class FakeGCSClient:
    """Stands in for `google.cloud.storage.Client` in tests."""

    def __init__(self):
        self._buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        if name not in self._buckets:
            self._buckets[name] = FakeBucket(name=name)
        return self._buckets[name]
