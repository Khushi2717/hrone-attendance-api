"""Integration tests that need a REAL MongoDB 6.0+ (aggregation pipelines, concurrency, explain).

    TEST_MONGO_URI=mongodb://localhost:27017 pytest tests/test_integration.py -v

Each test uses its own randomly named database that is dropped afterwards. They skip when no MongoDB is
reachable. The expected values below were worked out by hand from the rules R1-R10 (see comments)."""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from app import main as m


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def emp(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"N {code}", "email": f"{code}@x.io", "department": dept, "shift_start": start,
            "shift_end": end, "joined_on": joined, "created_at": utc(2026, 1, 1)}


def log(code, day, status="PRESENT", late=0, ot=0, wh=None, half=False, with_punch=True):
    rec = {"emp_code": code, "date": day, "status": status, "late_minutes": late, "overtime_minutes": ot,
           "work_hours": wh, "half_day": half, "history": [],
           "punch_in": utc(*map(int, day.split("-")), 4, 0) if with_punch and status in m.PRESENT_STATUSES else None,
           "punch_out": None}
    return rec


@pytest.fixture()
def seeded(real_db):
    """July 2026: Wed 07-01 ... Fri 07-31 -> 23 weekdays. Weekends: 4,5,11,12,18,19,25,26.
    Engineering has a mid-month joiner (EMP1002), an employee with no logs (EMP1003) and a future joiner (EMP1004)."""
    real_db.employees.insert_many([
        emp("EMP1001", "Engineering", "2026-06-01"), emp("EMP1002", "Engineering", "2026-07-13"),
        emp("EMP1003", "Engineering", "2026-07-01"), emp("EMP1004", "Engineering", "2026-08-05"),
        emp("EMP2001", "Sales", "2026-01-01"), emp("EMP2002", "Sales", "2026-01-01"),
        emp("EMP3001", "Ops", "2026-09-01"),  # whole department joins after July
    ])
    real_db.attendance_logs.insert_many([
        log("EMP1001", "2026-07-01", wh=9.0),
        log("EMP1001", "2026-07-02", "WFH", wh=4.0, half=True),
        log("EMP1001", "2026-07-03", "ON_DUTY", late=15, wh=8.0),
        log("EMP1001", "2026-07-04", late=20, ot=45, wh=5.0),                 # Saturday
        log("EMP1001", "2026-07-06", "LEAVE"),
        log("EMP1001", "2026-07-07", "ABSENT"),
        log("EMP1002", "2026-07-13", wh=8.5),
        log("EMP1002", "2026-07-14", late=30, wh=9.5),
        log("EMP1002", "2026-07-18", "ON_DUTY", wh=6.0),                      # Saturday
        log("EMP2001", "2026-07-08", late=30, wh=8.0),
        log("EMP2002", "2026-07-09", late=10, wh=8.0),
        log("EMP9999", "2026-07-10", late=500, wh=8.0),                        # unknown employee: ignored
        log("EMP1001", "2026-08-03", late=99, wh=8.0),                         # other month: ignored
    ])
    return real_db


def _client():
    from fastapi.testclient import TestClient
    return TestClient(m.app)


# ---------------------------------------------------------------- analytics ----------------------------------
def test_employee_monthly(seeded):
    c = _client()
    r = c.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    # present: 07-01 (1) + 07-02 half (0.5) + 07-03 (1); Saturday record and LEAVE/ABSENT excluded -> 2.5
    # late: 07-03 (15) + Saturday 07-04 (20) -> 2 records, 35 min; overtime 45 (Saturday counts)
    assert r.json() == {"emp_code": "EMP1001", "month": "2026-07", "working_days": 23, "present_days": 2.5,
                        "leave_days": 1, "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 45,
                        "attendance_pct": 10.87}  # 2.5/23*100 = 10.8695..


def test_employee_monthly_mid_month_joiner_and_no_logs(seeded):
    c = _client()
    r = c.get("/analytics/employees/EMP1002/monthly", params={"month": "2026-07"}).json()
    assert r["working_days"] == 15 and r["present_days"] == 2.0 and r["attendance_pct"] == 13.33  # 07-13..07-31
    r = c.get("/analytics/employees/EMP1003/monthly", params={"month": "2026-07"}).json()
    assert r["present_days"] == 0 and r["late_count"] == 0 and r["attendance_pct"] == 0.0
    r = c.get("/analytics/employees/EMP1004/monthly", params={"month": "2026-07"}).json()  # joins in August
    assert r["working_days"] == 0 and r["attendance_pct"] is None
    assert c.get("/analytics/employees/EMP0000/monthly", params={"month": "2026-07"}).status_code == 404
    assert c.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-7"}).status_code == 422


def test_department_summary(seeded):
    c = _client()
    r = c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert r["month"] == "2026-07"
    # Ops (joins in September) is omitted; EMP1004 (joins in August) is not counted.
    assert [i["department"] for i in r["items"]] == ["Engineering", "Sales"]
    eng, sales = r["items"]
    # present 2.5 + 2 = 4.5; late 2 + 1 = 3 records / 65 min; 1 leave; ON_DUTY on 07-03 and Saturday 07-18
    # work hours over present-status records: (9+4+8+5+8.5+9.5+6)/7 = 7.142857 -> 7.14 (average over records)
    assert eng == {"department": "Engineering", "headcount": 3, "present_days": 4.5, "avg_work_hours": 7.14,
                   "late_count": 3, "total_late_minutes": 65, "leave_count": 1, "on_duty_count": 2}
    # Sales: two people, one record each (EMP2001 08, EMP2002 09); present_days 2 (both weekdays)
    assert sales["headcount"] == 2 and sales["present_days"] == 2.0 and sales["avg_work_hours"] == 8.0
    only = c.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert [i["department"] for i in only["items"]] == ["Sales"]
    # a month in which nobody has logs still reports headcount (no-log employees count)
    r = c.get("/analytics/departments/summary", params={"month": "2026-06"}).json()
    assert {i["department"]: i["headcount"] for i in r["items"]} == {"Engineering": 1, "Sales": 2}
    assert next(i for i in r["items"] if i["department"] == "Sales")["avg_work_hours"] is None


def test_leaderboard_ties_and_cutoff(seeded):
    c = _client()
    # totals: EMP1001 35, EMP1002 30, EMP2001 30, EMP2002 10  -> ranks 1, 2, 2, 4 (EMP9999 ignored, EMP1003 has 0)
    rows = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 10}).json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP1001"), (2, "EMP1002"), (2, "EMP2001"), (4, "EMP2002")]
    assert rows[0] == {"rank": 1, "emp_code": "EMP1001", "name": "N EMP1001", "department": "Engineering",
                       "total_late_minutes": 35, "late_count": 2}
    # limit=2 cuts through the tie at rank 2 -> both tied employees are returned (3 rows)
    rows = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [r["emp_code"] for r in rows] == ["EMP1001", "EMP1002", "EMP2001"]
    # limit=3: rank 4 is beyond the limit even though only 3 rows precede it
    rows = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()["items"]
    assert [r["rank"] for r in rows] == [1, 2, 2]
    rows = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Sales"}).json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP2001"), (2, "EMP2002")]
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-05"}).json()["items"] == []
    for bad in ({"limit": 0}, {"limit": 51}):
        assert c.get("/analytics/leaderboard/late", params={"month": "2026-07", **bad}).status_code == 422


def test_trend_gap_filling_and_moving_average(seeded):
    c = _client()
    r = c.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-14"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["date"] for i in items] == [f"2026-07-{d:02d}" for d in range(1, 15)]  # one row per calendar day
    rates = [i["attendance_rate"] for i in items]
    # Wed..Fri rates .5 .25 .5 | Sat, Sun None | Mon..Fri 0.0 (LEAVE, ABSENT, no record, no record, no record) | Sat, Sun None
    # | Mon 07-13 1/3 (headcount 3 once EMP1002 joined) | Tue 1/3
    assert rates == [0.5, 0.25, 0.5, None, None, 0.0, 0.0, 0.0, 0.0, 0.0, None, None, 0.3333, 0.3333]
    assert [i["headcount"] for i in items] == [2] * 12 + [3, 3]
    assert [i["is_working_day"] for i in items[:7]] == [True, True, True, False, False, True, True]
    assert items[3]["present_count"] == 1.0 and items[3]["late_count"] == 1   # Saturday record still counted
    assert items[1]["present_count"] == 0.5                                      # half day
    assert items[13]["late_count"] == 1
    assert [i["moving_avg_7d"] for i in items] == [0.5, 0.375, 0.4167, 0.4167, 0.4167, 0.3125, 0.25,
                                                   0.15, 0.1, 0.0, 0.0, 0.0, 0.0667, 0.1333]


def test_trend_null_window_and_errors(seeded):
    c = _client()
    items = c.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-04", "to": "2026-07-05"}).json()["items"]
    assert [(i["attendance_rate"], i["moving_avg_7d"]) for i in items] == [(None, None), (None, None)]
    # no records at all in that range for Sales -> still one row per day, working days rate 0.0
    items = c.get("/analytics/departments/Sales/trend", params={"from": "2026-03-02", "to": "2026-03-03"}).json()["items"]
    assert [(i["headcount"], i["present_count"], i["attendance_rate"]) for i in items] == [(2, 0, 0.0), (2, 0, 0.0)]
    assert c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert c.get("/analytics/departments/Sales/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert c.get("/analytics/departments/Sales/trend", params={"from": "2026-01-01", "to": "2026-06-01"}).status_code == 422
    assert c.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01"}).status_code == 422


# ---------------------------------------------------------------- concurrency ---------------------------------
async def _fire(method, url, bodies):
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        return await asyncio.gather(*[ac.request(method, url, json=b) for b in bodies])


def _ms(day, h, mi):
    return int(datetime(2026, 7, day, h, mi, tzinfo=m.IST).timestamp()) * 1000


def test_concurrent_duplicate_employee_creates(real_db):
    body = {"emp_code": "EMP0001", "name": "A", "email": "a@x.io", "department": "D", "joined_on": "2026-01-01"}
    codes = sorted(r.status_code for r in asyncio.run(_fire("POST", "/employees", [body] * 20)))
    assert codes == [201] + [409] * 19
    assert real_db.employees.count_documents({}) == 1


def test_concurrent_punch_ins_exactly_one_201(real_db):
    real_db.employees.insert_one(emp("EMP0001", "D", "2026-01-01"))
    body = {"emp_code": "EMP0001", "punched_at": _ms(6, 9, 30)}
    codes = sorted(r.status_code for r in asyncio.run(_fire("POST", "/attendance/punch-in", [body] * 25)))
    assert codes == [201] + [409] * 24
    assert real_db.attendance_logs.count_documents({}) == 1


def test_concurrent_punch_outs_exactly_one_200(real_db):
    real_db.employees.insert_one(emp("EMP0001", "D", "2026-01-01"))
    _client().post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": _ms(6, 9, 30)})
    body = {"emp_code": "EMP0001", "punched_at": _ms(6, 19, 0)}
    codes = sorted(r.status_code for r in asyncio.run(_fire("POST", "/attendance/punch-out", [body] * 25)))
    assert codes == [200] + [409] * 24
    rec = real_db.attendance_logs.find_one({})
    assert rec["overtime_minutes"] == 30 and rec["work_hours"] == 9.5 and rec["history"] == []


def test_concurrent_regularizations_lose_no_history(real_db):
    real_db.employees.insert_one(emp("EMP0001", "D", "2026-01-01"))
    _client().post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": _ms(6, 9, 30)})
    # every request sets a different punch_out, so every one of them is a real change
    bodies = [{"punch_out": _ms(6, 17, 0) + i * 60_000, "reason": f"fix number {i}", "regularized_by": f"hr{i}"}
              for i in range(8)]
    responses = asyncio.run(_fire("PATCH", "/attendance/EMP0001/2026-07-06", bodies))
    ok = [r for r in responses if r.status_code == 200]
    assert ok and all(r.status_code in (200, 409, 422) for r in responses)
    history = real_db.attendance_logs.find_one({})["history"]
    # every accepted update is in the audit trail, exactly once, and nothing else is
    assert sorted(h["by"] for h in history) == sorted(f"hr{b['reason'][-1]}" for b, r in zip(bodies, responses) if r.status_code == 200)
    # each entry's 'from' is the previous entry's 'to': the chain is unbroken
    chain = [h["changes"]["punch_out"] for h in history]
    assert all(chain[i]["from"] == chain[i - 1]["to"] for i in range(1, len(chain)))


# ---------------------------------------------------------------- explain / performance -----------------------
def _plan_text(resp):
    assert resp.status_code == 200, resp.text
    return json.dumps(resp.json()["explain"])


def test_explain_endpoints_use_index_scans(seeded):
    c = _client()
    calls = {
        "attendance_list": {"emp_code": "EMP1001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT"},
        "employee_monthly": {"emp_code": "EMP1001", "month": "2026-07"},
        "department_summary": {"month": "2026-07"},
        "late_leaderboard": {"month": "2026-07", "limit": 5},
        "department_trend": {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"},
    }
    for name, params in calls.items():
        r = c.get(f"/admin/explain/{name}", params=params)
        text = _plan_text(r)
        assert r.json()["endpoint"] == name and r.json()["collection"] in ("employees", "attendance_logs")
        assert "IXSCAN" in text and "COLLSCAN" not in text, name
    assert c.get("/admin/explain/department_trend", params={"department": "x"}).status_code == 422
    assert c.get("/admin/explain/unknown").status_code == 422


@pytest.mark.skipif(not __import__("os").getenv("RUN_PERF"), reason="set RUN_PERF=1 to seed ~100k records")
def test_explain_and_latency_on_100k_records(real_db, capsys):
    """Seeds ~100k attendance documents and records the measured latency and plan details."""
    import random
    import time
    from datetime import date, timedelta
    rnd = random.Random(7)
    depts = ["Engineering", "Sales", "Support", "Finance", "Ops"]
    emps = [emp(f"EMP{i:04d}", depts[i % 5], "2025-01-06") for i in range(1, 301)]
    real_db.employees.insert_many(emps)
    logs, d0 = [], date(2026, 1, 1)
    for e in emps:
        for k in range(340):
            day = d0 + timedelta(days=k)
            if rnd.random() < 0.07 or (day.weekday() >= 5 and rnd.random() < 0.9):
                continue  # missing records / weekends mostly empty
            late = rnd.choice([0, 0, 0, 12, 25, 60])
            logs.append(log(e["emp_code"], day.isoformat(), rnd.choice(["PRESENT"] * 8 + ["WFH", "ON_DUTY"]),
                            late=late, ot=rnd.choice([0, 0, 35]), wh=round(rnd.uniform(3, 10), 2)))
    real_db.attendance_logs.insert_many(logs)
    m.ensure_indexes()
    c, report = _client(), []
    cases = [("GET", "/attendance", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
             ("GET", "/analytics/employees/EMP0007/monthly", {"month": "2026-03"}),
             ("GET", "/analytics/departments/summary", {"month": "2026-03"}),
             ("GET", "/analytics/leaderboard/late", {"month": "2026-03"}),
             ("GET", "/analytics/departments/Sales/trend", {"from": "2026-03-01", "to": "2026-05-31"})]
    for method, url, params in cases:
        t = time.perf_counter()
        assert c.request(method, url, params=params).status_code == 200
        report.append(f"{url}: {(time.perf_counter() - t) * 1000:.0f} ms")
    with capsys.disabled():
        print(f"\n{len(logs)} attendance docs\n" + "\n".join(report))
    for name, params in {"attendance_list": {"date_from": "2026-03-01", "date_to": "2026-03-31"},
                         "employee_monthly": {"emp_code": "EMP0007", "month": "2026-03"},
                         "department_summary": {"month": "2026-03"}, "late_leaderboard": {"month": "2026-03"},
                         "department_trend": {"department": "Sales", "from": "2026-03-01", "to": "2026-05-31"}}.items():
        text = _plan_text(c.get(f"/admin/explain/{name}", params=params))
        assert "IXSCAN" in text and "COLLSCAN" not in text, name
