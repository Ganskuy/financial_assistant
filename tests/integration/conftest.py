import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.db import Database
from app.models.tables import Base
from app.repositories.updates import UpdateRepository


@pytest.fixture
async def db(settings):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set TEST_DATABASE_URL to a migrated, disposable PostgreSQL database ending in _test"
        )
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("Refusing to truncate a database whose name does not end in _test")
    database = Database(settings)
    # Static metadata allowlist, never supplied by a user/model.
    names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
    async with database.transaction() as session:
        await session.execute(text("TRUNCATE TABLE " + names + " CASCADE"))
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def users(db, settings):
    repository = UpdateRepository(db, settings)
    return await repository.user(1001), await repository.user(1002)
