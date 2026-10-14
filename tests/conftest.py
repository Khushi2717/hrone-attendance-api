"""Backends for the tests.

  sim   mongomock for CRUD + tests/minimongo.py (a SIMULATION of the aggregation operators). Always available.
  real  a throw-away database on a real MongoDB 6.0+ (TEST_MONGO_URI). Skipped when none is reachable.

Tests that take `backend_db` run on both, so the same assertions are the evidence for a real server as soon as one
is available. Evidence from the `sim` parameter is NOT evidence about MongoDB itself."""
import os
import uuid

import mongomock
import pytest
from pymongo import MongoClient

from app import main as m
from tests.minimongo import SimDB

TEST_URI = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")


_availability = {}  # probe the server once per test run, not once per test


def _real_client():
    if _availability.get("ok") is False:
        pytest.skip(f"no MongoDB reachable at TEST_MONGO_URI={TEST_URI!r}")
    client = MongoClient(TEST_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except Exception:  # noqa: BLE001 - any failure to connect means "no MongoDB here"
        _availability["ok"] = False
        pytest.skip(f"no MongoDB reachable at TEST_MONGO_URI={TEST_URI!r}")
    _availability["ok"] = True
    return client


@pytest.fixture()
def real_db(monkeypatch):
    client = _real_client()
    name = f"hrone_test_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(m, "client", client)
    monkeypatch.setattr(m, "db", client[name])
    m.ensure_indexes()
    yield client[name]
    client.drop_database(name)
    client.close()


@pytest.fixture(params=["sim", "real"])
def backend_db_bare(request, monkeypatch):
    """Empty database WITHOUT indexes (to test index creation over existing data)."""
    if request.param == "sim":
        db = SimDB(mongomock.MongoClient(tz_aware=True)["bare"])
        monkeypatch.setattr(m, "db", db)
        yield db
        return
    client = _real_client()
    name = f"hrone_test_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(m, "client", client)
    monkeypatch.setattr(m, "db", client[name])
    yield client[name]
    client.drop_database(name)
    client.close()


@pytest.fixture()
def backend_db(backend_db_bare):
    m.ensure_indexes()
    return backend_db_bare
