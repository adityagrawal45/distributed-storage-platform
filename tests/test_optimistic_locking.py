"""
Optimistic locking (Phase 4) — `FileMetadata.lock_version`.

Self-contained: builds its own in-memory SQLite schema rather than
reusing the `client`/`db_session` fixtures, because this test needs TWO
independent sessions racing against the SAME row — the HTTP `client`
fixture deliberately shares one session across a whole test (see
`tests/conftest.py`), which would hide the exact race this test exists
to prove.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError

from app.database.session import Base
from app.models.file_metadata import FileMetadata
from app.models.user import User, UserRole


@pytest_asyncio.fixture
async def race_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    owner_id = uuid.uuid4()
    async with session_factory() as setup_session:
        setup_session.add(
            User(
                id=owner_id,
                first_name="Race",
                last_name="Condition",
                email="race@nimbusfs.io",
                hashed_password="not-a-real-hash",
                role=UserRole.USER,
            )
        )
        file = FileMetadata(
            owner_id=owner_id,
            original_filename="report.pdf",
            stored_filename=f"{uuid.uuid4()}.pdf",
            extension="pdf",
            size=10,
            version=1,
        )
        setup_session.add(file)
        await setup_session.commit()
        file_id = file.id

    yield session_factory, file_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_updates_raise_stale_data_error(race_session_factory):
    """Two sessions load the same row; the second commit after the first must fail, not silently overwrite it."""
    session_factory, file_id = race_session_factory

    async with session_factory() as session_a, session_factory() as session_b:
        file_a = (await session_a.execute(select(FileMetadata).where(FileMetadata.id == file_id))).scalar_one()
        file_b = (await session_b.execute(select(FileMetadata).where(FileMetadata.id == file_id))).scalar_one()

        file_a.original_filename = "renamed-by-a.pdf"
        await session_a.commit()

        file_b.original_filename = "renamed-by-b.pdf"
        with pytest.raises(StaleDataError):
            await session_b.commit()


@pytest.mark.asyncio
async def test_sequential_updates_do_not_conflict(race_session_factory):
    """The common case — no concurrency — must keep working exactly as before."""
    session_factory, file_id = race_session_factory

    async with session_factory() as session:
        file = (await session.execute(select(FileMetadata).where(FileMetadata.id == file_id))).scalar_one()
        assert file.lock_version == 1
        file.original_filename = "first-rename.pdf"
        await session.commit()
        assert file.lock_version == 2

        file.original_filename = "second-rename.pdf"
        await session.commit()
        assert file.lock_version == 3
