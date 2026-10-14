"""Analytics endpoints. Runs on the `sim` backend (aggregation SIMULATOR) and, when a MongoDB is reachable,
on the `real` backend. Three independent sources of truth are compared: hand-computed values, the pure-Python
oracle (tests/oracle.py) and the API."""
import random
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from tests import helpers as h
from tests import oracle


@pytest.fixture()
def client():
    from app import main as m
    return TestClient(m.app)


@pytest.fixture()
def seeded(backend_db):
    h.seed_july(backend_db)
    return backend_db


def raw(db):
    return list(db.employees.find({}, {"_id": 0})), list(db.attendance_logs.find({}, {"_id": 0}))


# ------------------------------------------------------------- hand-computed values
def test_employee_monthly_hand_values(seeded, client):
    r = client.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    assert r.json() == {"emp_code": "EMP1001", "month": "2026-07", "working_days": 23, "present_days": 2.5,
                        "leave_days": 1, "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 45,
                        "attendance_pct": 10.87}


def test_employee_monthly_joiner_no_logs_future_and_errors(seeded, client):
    g = lambda code, month="2026-07": client.get(f"/analytics/employees/{code}/monthly", params={"month": month})  # noqa: E731
    r = g("EMP1002").json()
    assert (r["working_days"], r["present_days"], r["attendance_pct"]) == (15, 2.0, 13.33)  # 07-13..07-31, Sat excluded
    r = g("EMP1003").json()
    assert (r["present_days"], r["late_count"], r["attendance_pct"]) == (0.0, 0, 0.0)
    r = g("EMP1004").json()
    assert (r["working_days"], r["attendance_pct"]) == (0, None)  # joins in August: zero denominator -> null
    assert g("EMP0000").status_code == 404
    assert g("EMP1001", "2026-7").status_code == 422 and g("EMP1001", "2026-13").status_code == 422
    assert client.get("/analytics/employees/EMP1001/monthly").status_code == 422


def test_department_summary_hand_values(seeded, client):
    r = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert [i["department"] for i in r["items"]] == ["Engineering", "Sales"]  # Ops joins in September: omitted
    assert r["items"][0] == {"department": "Engineering", "headcount": 3, "present_days": 4.5, "avg_work_hours": 7.14,
                             "late_count": 3, "total_late_minutes": 65, "leave_count": 1, "on_duty_count": 2}
    assert r["items"][1] == {"department": "Sales", "headcount": 2, "present_days": 2.0, "avg_work_hours": 8.0,
                             "late_count": 2, "total_late_minutes": 40, "leave_count": 0, "on_duty_count": 0}
    only = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert [i["department"] for i in only["items"]] == ["Sales"]
    june = client.get("/analytics/departments/summary", params={"month": "2026-06"}).json()["items"]
    assert [(i["department"], i["headcount"], i["avg_work_hours"], i["present_days"]) for i in june] == \
        [("Engineering", 1, None, 0.0), ("Sales", 2, None, 0.0)]       # no-log employees still counted
    assert client.get("/analytics/departments/summary", params={"month": "1999-01"}).json()["items"] == []
    assert client.get("/analytics/departments/summary").status_code == 422


def test_leaderboard_ties_cutoff_filters(seeded, client):
    lb = lambda **p: client.get("/analytics/leaderboard/late", params={"month": "2026-07", **p})  # noqa: E731
    rows = lb(limit=10).json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP1001"), (2, "EMP1002"), (2, "EMP2001"), (4, "EMP2002")]
    assert rows[0] == {"rank": 1, "emp_code": "EMP1001", "name": "N EMP1001", "department": "Engineering",
                       "total_late_minutes": 35, "late_count": 2}
    assert [r["emp_code"] for r in lb(limit=2).json()["items"]] == ["EMP1001", "EMP1002", "EMP2001"]  # tie at cutoff
    assert [r["rank"] for r in lb(limit=3).json()["items"]] == [1, 2, 2]                              # rank 4 > 3
    assert [r["rank"] for r in lb(limit=1).json()["items"]] == [1]
    assert [(r["rank"], r["emp_code"]) for r in lb(department="Sales").json()["items"]] == [(1, "EMP2001"), (2, "EMP2002")]
    assert lb(department="Nope").json()["items"] == []
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-05"}).json()["items"] == []
    assert len(lb().json()["items"]) == 4  # default limit 10
    for bad in ({"limit": 0}, {"limit": 51}, {"limit": "x"}):
        assert lb(**bad).status_code == 422


def test_trend_hand_values(seeded, client):
    r = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-14"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["date"] for i in items] == [f"2026-07-{d:02d}" for d in range(1, 15)]
    assert [i["attendance_rate"] for i in items] == [0.5, 0.25, 0.5, None, None, 0.0, 0.0, 0.0, 0.0, 0.0, None, None, 0.3333, 0.3333]
    assert [i["headcount"] for i in items] == [2] * 12 + [3, 3]
    assert [i["is_working_day"] for i in items[:7]] == [True, True, True, False, False, True, True]
    assert (items[3]["present_count"], items[3]["late_count"]) == (1.0, 1)   # Saturday record counted
    assert items[1]["present_count"] == 0.5 and items[13]["late_count"] == 1
    assert [i["moving_avg_7d"] for i in items] == [0.5, 0.375, 0.4167, 0.4167, 0.4167, 0.3125, 0.25,
                                                   0.15, 0.1, 0.0, 0.0, 0.0, 0.0667, 0.1333]
    assert set(items[0]) == {"date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"}


def test_trend_edge_cases(seeded, client):
    t = lambda dept, a, b: client.get(f"/analytics/departments/{dept}/trend", params={"from": a, "to": b})  # noqa: E731
    items = t("Engineering", "2026-07-04", "2026-07-05").json()["items"]   # Sat+Sun: no rate -> null window
    assert [(i["attendance_rate"], i["moving_avg_7d"]) for i in items] == [(None, None)] * 2
    items = t("Sales", "2026-03-02", "2026-03-03").json()["items"]          # no records at all
    assert [(i["headcount"], i["present_count"], i["attendance_rate"], i["moving_avg_7d"]) for i in items] == [(2, 0, 0.0, 0.0)] * 2
    assert len(t("Sales", "2026-07-01", "2026-07-01").json()["items"]) == 1   # one-day range
    assert len(t("Sales", "2026-07-01", "2026-09-30").json()["items"]) == 92  # maximum allowed
    items = t("Engineering", "2026-08-01", "2026-08-07").json()["items"]      # EMP1004 joins 08-05 -> headcount 3 -> 4
    assert [i["headcount"] for i in items] == [3, 3, 3, 3, 4, 4, 4]
    # a department whose only employee joins mid-range: headcount 0 before -> rate null, not a divide-by-zero
    items = t("Ops", "2026-08-31", "2026-09-02").json()["items"]
    assert [(i["headcount"], i["attendance_rate"]) for i in items] == [(0, None), (1, 0.0), (1, 0.0)]
    assert t("Nope", "2026-07-01", "2026-07-02").status_code == 404
    assert t("Sales", "2026-07-02", "2026-07-01").status_code == 422
    assert t("Sales", "2026-07-01", "2026-10-01").status_code == 422      # 93 days
    assert t("Sales", "2026-02-30", "2026-03-01").status_code == 422
    assert client.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01"}).status_code == 422


def test_missing_optional_fields_in_seeded_data(backend_db, client):
    """Legacy documents may omit late_minutes / overtime_minutes / half_day / history / work_hours."""
    backend_db.employees.insert_one(h.emp("EMP0001", "Eng", "2026-01-05"))
    legacy = {"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT", "punch_in": h.utc(2026, 7, 14, 4, 2)}
    backend_db.attendance_logs.insert_many([legacy, {**legacy, "date": "2026-07-15", "work_hours": 8.0}])
    m = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert (m["present_days"], m["late_count"], m["total_late_minutes"], m["total_overtime_minutes"]) == (2.0, 0, 0, 0)
    s = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert (s["present_days"], s["avg_work_hours"]) == (2.0, 8.0)   # only the record WITH work_hours is averaged
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"] == []
    t = client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-14", "to": "2026-07-15"}).json()["items"]
    assert [i["present_count"] for i in t] == [1.0, 1.0]


def test_boundaries_first_and_last_day_of_month_and_leap_february(backend_db, client):
    backend_db.employees.insert_one(h.emp("EMP0001", "Eng", "2024-01-01"))
    backend_db.attendance_logs.insert_many([h.log("EMP0001", d, wh=8.0, late=20) for d in
                                            ("2024-01-31", "2024-02-01", "2024-02-29", "2024-03-01")])
    m = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2024-02"}).json()
    assert (m["working_days"], m["present_days"], m["late_count"]) == (21, 2.0, 2)   # Feb 2024: 29 days, 21 weekdays
    lb = client.get("/analytics/leaderboard/late", params={"month": "2024-02"}).json()["items"]
    assert lb[0]["total_late_minutes"] == 40


# ------------------------------------------------------------- randomized differential test vs the oracle
def random_world(seed):
    rnd = random.Random(seed)
    depts = ["Engineering", "Sales", "Support"]
    emps = [h.emp(f"EMP{1000 + i}", rnd.choice(depts), (date(2026, 5, 20) + timedelta(days=rnd.randrange(0, 100))).isoformat())
            for i in range(14)]
    logs, seen = [], set()
    for e in emps:
        first = date.fromisoformat(e["joined_on"])
        for k in range(-3, 45):
            day = date(2026, 6, 28) + timedelta(days=k)
            if day < first or rnd.random() < (0.35 if day.weekday() < 5 else 0.85):
                continue
            status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [10, 3, 2, 1, 2])[0]
            present = status in ("PRESENT", "WFH", "ON_DUTY")
            rec = h.log(e["emp_code"], day.isoformat(), status,
                        late=rnd.choice([0, 0, 0, 12, 30, 30, 75]) if present else 0,
                        ot=rnd.choice([0, 0, 30, 45]) if present else 0,
                        wh=round(rnd.uniform(2, 11), 2) if (present and rnd.random() < 0.8) or (not present and rnd.random() < 0.3) else None,
                        half=present and rnd.random() < 0.15)
            for legacy_field in ("late_minutes", "overtime_minutes", "half_day", "history"):   # missing optional fields
                if rnd.random() < 0.1:
                    rec.pop(legacy_field)
            if (e["emp_code"], rec["date"]) not in seen:
                seen.add((e["emp_code"], rec["date"]))
                logs.append(rec)
    logs.append(h.log("EMP9999", "2026-07-10", late=500, wh=8.0))   # employee does not exist
    return emps, logs


@pytest.mark.parametrize("seed", range(6))
def test_random_worlds_match_oracle(backend_db, client, seed):
    emps, logs = random_world(seed)
    backend_db.employees.insert_many([dict(e) for e in emps])
    backend_db.attendance_logs.insert_many([dict(r) for r in logs])
    e_all, l_all = raw(backend_db)
    rnd = random.Random(seed + 100)
    for month in ("2026-06", "2026-07", "2026-08"):
        for e in emps:
            got = client.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": month}).json()
            assert got == oracle.monthly(e_all, l_all, e["emp_code"], month), (month, e["emp_code"])
        assert client.get("/analytics/departments/summary", params={"month": month}).json() == \
            oracle.department_summary(e_all, l_all, month)
        assert client.get("/analytics/departments/summary", params={"month": month, "department": "Sales"}).json() == \
            oracle.department_summary(e_all, l_all, month, "Sales")
        for limit in (1, 2, 3, 10, 50):
            for dept in (None, "Engineering"):
                params = {"month": month, "limit": limit, **({"department": dept} if dept else {})}
                assert client.get("/analytics/leaderboard/late", params=params).json() == \
                    oracle.leaderboard(e_all, l_all, month, limit, dept), params
    for dept in ("Engineering", "Sales", "Support"):
        for _ in range(4):
            a = date(2026, 6, 25) + timedelta(days=rnd.randrange(0, 40))
            b = a + timedelta(days=rnd.randrange(0, 40))
            got = client.get(f"/analytics/departments/{dept}/trend", params={"from": a.isoformat(), "to": b.isoformat()})
            assert got.status_code == 200
            assert got.json() == oracle.trend(e_all, l_all, dept, a, b), (dept, a, b)


def test_avg_work_hours_ignores_non_present_statuses_and_missing_hours(backend_db, client):
    """openapi: avg over records that are present-status AND have a work_hours value."""
    backend_db.employees.insert_one(h.emp("EMP0001", "Eng", "2026-01-05"))
    backend_db.attendance_logs.insert_many([
        h.log("EMP0001", "2026-07-06", wh=8.0), h.log("EMP0001", "2026-07-07", "WFH", wh=6.0),
        h.log("EMP0001", "2026-07-08", "LEAVE", wh=99.0),      # stale hours on a LEAVE record must not be averaged
        h.log("EMP0001", "2026-07-09", "ABSENT", wh=50.0),
        h.log("EMP0001", "2026-07-10"),                         # present but still open: work_hours null
    ])
    item = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert item["avg_work_hours"] == 7.0 and item["leave_count"] == 1 and item["present_days"] == 3.0
