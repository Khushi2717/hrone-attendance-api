import os
import uuid

import pytest
from pymongo import MongoClient

from app import main as m

TEST_URI = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")


@pytest.fixture()
def real_db(monkeypatch):
    """A throw-away database on a real MongoDB (6.0+). Skips when none is reachable.
    The database name is random and dropped afterwards; nothing else on the server is touched."""
    client = MongoClient(TEST_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except Exception:  # noqa: BLE001 - any failure to connect means "no MongoDB here"
        pytest.skip(f"no MongoDB reachable at TEST_MONGO_URI={TEST_URI!r}")
    name = f"hrone_test_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(m, "client", client)
    monkeypatch.setattr(m, "db", client[name])
    m.ensure_indexes()
    yield client[name]
    client.drop_database(name)
    client.close()
