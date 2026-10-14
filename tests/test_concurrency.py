"""Parallel requests through the ASGI app (sync endpoints run in a thread pool, so they truly overlap).

On `sim` (mongomock) this exercises the APPLICATION logic: unique-key mapping to 409, conditional updates, the
optimistic-concurrency retry loop. It is NOT evidence that MongoDB itself is atomic; that evidence only comes from
the `real` parameter, which is skipped when no MongoDB is reachable."""
import asyncio
from datetime import datetime

import httpx
import pytest

from app import main as m
from tests import helpers as h

N = 24


async def fire(method, url, bodies):
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        return await asyncio.gather(*[ac.request(method, url, json=b) for b in bodies])


def ms(day, hour, minute):
    return int(datetime(2026, 7, day, hour, minute, tzinfo=m.IST).timestamp()) * 1000


@pytest.mark.parametrize("round_no", range(3))
def test_concurrent_duplicate_employee_creates(backend_db, round_no):
    body = {"emp_code": "EMP0001", "name": "A", "email": "a@x.io", "department": "D", "joined_on": "2026-01-01"}
    codes = sorted(r.status_code for r in asyncio.run(fire("POST", "/employees", [body] * N)))
    assert codes == [201] + [409] * (N - 1)
    assert backend_db.employees.count_documents({}) == 1


@pytest.mark.parametrize("round_no", range(3))
def test_concurrent_punch_ins_exactly_one_201(backend_db, round_no):
    backend_db.employees.insert_one(h.emp("EMP0001", "D", "2026-01-01"))
    body = {"emp_code": "EMP0001", "punched_at": ms(6, 9, 30)}
    resp = asyncio.run(fire("POST", "/attendance/punch-in", [body] * N))
    assert sorted(r.status_code for r in resp) == [201] + [409] * (N - 1)
    assert backend_db.attendance_logs.count_documents({}) == 1
    # a different instant on the same attendance date is also a conflict (R1: one record per employee and date)
    other = {"emp_code": "EMP0001", "punched_at": ms(6, 14, 0)}
    assert sorted(r.status_code for r in asyncio.run(fire("POST", "/attendance/punch-in", [other] * 5))) == [409] * 5
    assert backend_db.attendance_logs.count_documents({}) == 1


def test_concurrent_punch_ins_for_different_days_all_succeed(backend_db):
    backend_db.employees.insert_one(h.emp("EMP0001", "D", "2026-01-01"))
    bodies = [{"emp_code": "EMP0001", "punched_at": ms(d, 9, 30)} for d in range(1, 21)]
    resp = asyncio.run(fire("POST", "/attendance/punch-in", bodies))
    assert all(r.status_code == 201 for r in resp)
    assert backend_db.attendance_logs.count_documents({}) == 20


@pytest.mark.parametrize("round_no", range(3))
def test_concurrent_punch_outs_exactly_one_200(backend_db, round_no):
    backend_db.employees.insert_one(h.emp("EMP0001", "D", "2026-01-01"))
    assert asyncio.run(fire("POST", "/attendance/punch-in", [{"emp_code": "EMP0001", "punched_at": ms(6, 9, 30)}]))[0].status_code == 201
    body = {"emp_code": "EMP0001", "punched_at": ms(6, 19, 0)}
    resp = asyncio.run(fire("POST", "/attendance/punch-out", [body] * N))
    assert sorted(r.status_code for r in resp) == [200] + [409] * (N - 1)
    docs = list(backend_db.attendance_logs.find({}))
    assert len(docs) == 1
    assert (docs[0]["overtime_minutes"], docs[0]["work_hours"], docs[0]["history"]) == (30, 9.5, [])
    assert backend_db.attendance_logs.count_documents({"punch_out": None}) == 0


def test_concurrent_punch_outs_with_different_times_exactly_one_wins(backend_db):
    backend_db.employees.insert_one(h.emp("EMP0001", "D", "2026-01-01"))
    asyncio.run(fire("POST", "/attendance/punch-in", [{"emp_code": "EMP0001", "punched_at": ms(6, 9, 30)}]))
    bodies = [{"emp_code": "EMP0001", "punched_at": ms(6, 17, 0) + i * 60_000} for i in range(N)]
    resp = asyncio.run(fire("POST", "/attendance/punch-out", bodies))
    winners = [(b, r.json()) for b, r in zip(bodies, resp, strict=True) if r.status_code == 200]
    assert len(winners) == 1 and all(r.status_code in (200, 409) for r in resp)
    stored = backend_db.attendance_logs.find_one({})
    assert m.dt_to_ms(stored["punch_out"]) == winners[0][0]["punched_at"] == winners[0][1]["punch_out"]


@pytest.mark.parametrize("round_no", range(3))
def test_concurrent_regularizations_lose_no_history(backend_db, round_no):
    backend_db.employees.insert_one(h.emp("EMP0001", "D", "2026-01-01"))
    asyncio.run(fire("POST", "/attendance/punch-in", [{"emp_code": "EMP0001", "punched_at": ms(6, 9, 30)}]))
    # every request sets a different punch_out, so every accepted one is a real change
    bodies = [{"punch_out": ms(6, 17, 0) + i * 60_000, "reason": f"fix number {i}", "regularized_by": f"hr{i}"} for i in range(8)]
    resp = asyncio.run(fire("PATCH", "/attendance/EMP0001/2026-07-06", bodies))
    assert all(r.status_code in (200, 409) for r in resp), [r.status_code for r in resp]   # never 500/422
    accepted = [i for i, r in enumerate(resp) if r.status_code == 200]
    assert accepted
    stored = backend_db.attendance_logs.find_one({})
    # exactly the accepted updates are in the audit trail, once each, nothing else
    assert sorted(e["by"] for e in stored["history"]) == sorted(f"hr{i}" for i in accepted)
    # the audit trail is an unbroken chain: each entry's `from` is the previous entry's `to` (no lost update)
    chain = [e["changes"]["punch_out"] for e in stored["history"]]
    assert chain[0]["from"] is None
    assert all(chain[i]["from"] == chain[i - 1]["to"] for i in range(1, len(chain)))
    assert m.dt_to_ms(stored["punch_out"]) == m.dt_to_ms(chain[-1]["to"])
    # derived fields are consistent with the final punch times
    final_out = stored["punch_out"]
    assert stored["work_hours"] == m.compute_work_hours(stored["punch_in"], final_out)


def test_regularization_racing_with_punch_out_keeps_state_consistent(backend_db):
    backend_db.employees.insert_one(h.emp("EMP0001", "D", "2026-01-01"))
    asyncio.run(fire("POST", "/attendance/punch-in", [{"emp_code": "EMP0001", "punched_at": ms(6, 10, 5)}]))

    async def both():
        transport = httpx.ASGITransport(app=m.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            return await asyncio.gather(
                ac.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(6, 19, 10)}),
                ac.patch("/attendance/EMP0001/2026-07-06", json={"punch_in": ms(6, 9, 28), "reason": "biometric glitch", "regularized_by": "hr"}))
    out, patch = asyncio.run(both())
    assert out.status_code in (200, 409) and patch.status_code in (200, 409, 422)
    s = backend_db.attendance_logs.find_one({})
    # whatever the interleaving, stored derived fields must match the stored punch times
    if s.get("punch_out") is not None:
        assert s["work_hours"] == m.compute_work_hours(s["punch_in"], s["punch_out"])
    assert s["late_minutes"] == m.compute_late_minutes(s["punch_in"], m.date.fromisoformat(s["date"]), "09:30")
    assert len(s["history"]) == (1 if patch.status_code == 200 else 0)
