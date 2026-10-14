"""Deterministic race-window tests.

The stress tests in test_concurrency.py cannot reliably force an interleaving (on the mongomock backend they have
no power at all: removing the atomic guards does not make them fail). These tests instead INJECT a competing write
exactly between the application's read and its write, which is the window a check-then-act bug would lose in.
They prove the application's guards (conditional updates, unique keys, optimistic retry) do their job; MongoDB's own
single-document atomicity is a documented guarantee that only the `real` backend can demonstrate."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main as m
from tests import helpers as h


def ms(day, hour, minute, sec=0):
    return int(datetime(2026, 7, day, hour, minute, sec, tzinfo=m.IST).timestamp()) * 1000


class Hooked:
    """Delegating wrapper around a collection: runs `fn` once per call right after find_one (or right before
    insert_one), never re-entrantly, at most `times` times."""

    def __init__(self, coll, fn, when="after_find_one", times=1):
        self._c, self._fn, self._when, self._left, self._busy = coll, fn, when, times, False

    def _fire(self):
        if self._left > 0 and not self._busy:
            self._busy, self._left = True, self._left - 1
            try:
                self._fn()
            finally:
                self._busy = False

    def find_one(self, *a, **k):
        result = self._c.find_one(*a, **k)
        if self._when == "after_find_one":
            self._fire()
        return result

    def insert_one(self, *a, **k):
        if self._when == "before_insert_one":
            self._fire()
        return self._c.insert_one(*a, **k)

    def __getattr__(self, name):
        return getattr(self._c, name)


class DBWithHook:
    def __init__(self, db, coll_name, fn, **kw):
        self._db, self._name = db, coll_name
        self._hooked = Hooked(getattr(db, coll_name), fn, **kw)

    def __getattr__(self, name):
        return self._hooked if name == self._name else getattr(self._db, name)


@pytest.fixture()
def world(backend_db):
    backend_db.employees.insert_one(h.emp("EMP0001", "D", "2026-01-01"))
    c = TestClient(m.app)
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(6, 10, 5)}).status_code == 201
    return backend_db, c


def hook_in(monkeypatch, db, fn, **kw):
    monkeypatch.setattr(m, "db", DBWithHook(db, "attendance_logs", fn, **kw))


def patch(c, **body):
    return c.patch("/attendance/EMP0001/2026-07-06", json={"reason": "valid reason", "regularized_by": "hr", **body})


def test_punch_out_loses_race_cleanly(world, monkeypatch):
    db, c = world
    competing = lambda: assert_status(c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(6, 18, 0)}), 200)  # noqa: E731
    hook_in(monkeypatch, db, competing)
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(6, 19, 30)})
    assert r.status_code == 409                                  # the guard rejects the stale write
    stored = db.attendance_logs.find_one({})
    assert m.dt_to_ms(stored["punch_out"]) == ms(6, 18, 0)       # the winner's punch-out was NOT overwritten
    assert stored["overtime_minutes"] == 0 and stored["history"] == []


def assert_status(resp, code):
    assert resp.status_code == code, resp.text


def test_punch_in_duplicate_decided_by_unique_index_not_by_a_prior_read(world, monkeypatch):
    db, c = world
    db.attendance_logs.delete_many({})
    competing = lambda: assert_status(c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(6, 9, 31)}), 201)  # noqa: E731
    hook_in(monkeypatch, db, competing, when="before_insert_one")
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(6, 9, 30)})
    assert r.status_code == 409
    assert db.attendance_logs.count_documents({}) == 1
    assert m.dt_to_ms(db.attendance_logs.find_one({})["punch_in"]) == ms(6, 9, 31)


def test_regularize_vs_regularize_keeps_both_audit_entries(world, monkeypatch):
    db, c = world
    competing = lambda: assert_status(patch(c, punch_out=ms(6, 17, 0), regularized_by="hrB"), 200)  # noqa: E731
    hook_in(monkeypatch, db, competing)
    r = patch(c, punch_out=ms(6, 19, 0), regularized_by="hrA")
    assert r.status_code == 200                                  # retried on top of the competitor's change
    body = r.json()
    assert [e["by"] for e in body["history"]] == ["hrB", "hrA"]
    first, second = (e["changes"]["punch_out"] for e in body["history"])
    assert first == {"from": None, "to": ms(6, 17, 0)}
    assert second == {"from": ms(6, 17, 0), "to": ms(6, 19, 0)}  # A saw B's value as its "before": nothing was lost
    assert body["punch_out"] == ms(6, 19, 0) and body["overtime_minutes"] == 30


def test_regularize_vs_punch_out_does_not_erase_the_punch_out(world, monkeypatch):
    db, c = world
    competing = lambda: assert_status(c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(6, 19, 10)}), 200)  # noqa: E731
    hook_in(monkeypatch, db, competing)
    r = patch(c, punch_in=ms(6, 9, 28), reason="biometric glitch")
    assert r.status_code == 200
    b = r.json()
    assert b["punch_out"] == ms(6, 19, 10)                      # punch-out survived
    assert (b["work_hours"], b["late_minutes"], b["overtime_minutes"]) == (9.7, 0, 40)    # 09:28 -> 19:10 = 9h42m, recomputed from BOTH changes
    assert len(b["history"]) == 1 and set(b["history"][0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}  # overtime stayed 40: not a change


def test_regularize_gives_up_with_409_not_500_when_always_beaten(world, monkeypatch):
    db, c = world
    counter = iter(range(1, 100))
    competing = lambda: assert_status(patch(c, punch_out=ms(6, 17, 0) + next(counter) * 60_000, regularized_by="rival"), 200)  # noqa: E731
    hook_in(monkeypatch, db, competing, times=m.REGULARIZE_MAX_ATTEMPTS)
    r = patch(c, punch_out=ms(6, 22, 0), regularized_by="slow")
    assert r.status_code == 409
    stored = db.attendance_logs.find_one({})
    assert len(stored["history"]) == m.REGULARIZE_MAX_ATTEMPTS and all(e["by"] == "rival" for e in stored["history"])


def test_regularize_legacy_record_without_history_field(backend_db):
    backend_db.employees.insert_one(h.emp("EMP0001", "D", "2026-01-01"))
    rec = h.log("EMP0001", "2026-07-06", wh=None)
    for f in ("history", "half_day"):
        rec.pop(f)
    rec["punch_in"] = m.ms_to_dt(ms(6, 10, 5))
    backend_db.attendance_logs.insert_one(rec)
    c = TestClient(m.app)
    r = patch(c, punch_out=ms(6, 19, 10))
    assert r.status_code == 200 and len(r.json()["history"]) == 1
    assert patch(c, status="WFH").json()["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}
