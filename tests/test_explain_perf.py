"""Real-MongoDB-only checks: explain plans (IXSCAN, no COLLSCAN) and the 100k-record performance run.
These cannot be simulated. They skip when no MongoDB is reachable at TEST_MONGO_URI."""
import json
import os
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main as m
from tests import helpers as h

TARGETS = {
    "attendance_list": {"emp_code": "EMP1001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT"},
    "employee_monthly": {"emp_code": "EMP1001", "month": "2026-07"},
    "department_summary": {"month": "2026-07"},
    "late_leaderboard": {"month": "2026-07", "limit": 5},
    "department_trend": {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"},
}


def plan_text(resp):
    assert resp.status_code == 200, resp.text
    return json.dumps(resp.json()["explain"])


def test_explain_on_small_data_and_variants(real_db):
    h.seed_july(real_db)
    c = TestClient(m.app)
    for name, params in TARGETS.items():
        r = c.get(f"/admin/explain/{name}", params=params)
        text = plan_text(r)
        assert r.json()["endpoint"] == name and r.json()["collection"] in ("employees", "attendance_logs")
        assert "IXSCAN" in text and "COLLSCAN" not in text, (name, text[:400])
        assert '"executionStats"' in text
    variants = [("attendance_list", {}), ("attendance_list", {"status": "WFH"}), ("attendance_list", {"date_from": "2026-07-01"}),
                ("attendance_list", {"emp_code": "EMP1001"}), ("department_summary", {"month": "2026-07", "department": "Sales"}),
                ("late_leaderboard", {"month": "2026-07", "department": "Sales"})]
    for name, params in variants:
        text = plan_text(c.get(f"/admin/explain/{name}", params=params))
        assert "IXSCAN" in text and "COLLSCAN" not in text, (name, params)
    assert c.get("/admin/explain/department_trend", params={"department": "x"}).status_code == 422
    assert c.get("/admin/explain/employee_monthly").status_code == 422
    assert c.get("/admin/explain/unknown").status_code == 422


@pytest.mark.skipif(not os.getenv("RUN_PERF"), reason="set RUN_PERF=1 to seed ~100k records")
def test_explain_and_latency_on_100k_records(real_db, capsys):
    import random
    rnd = random.Random(7)
    depts = ["Engineering", "Sales", "Support", "Finance", "Ops"]
    emps = [h.emp(f"EMP{i:04d}", depts[i % 5], "2025-01-06") for i in range(1, 301)]
    real_db.employees.insert_many(emps)
    logs, d0 = [], date(2026, 1, 1)
    for e in emps:
        for k in range(340):
            day = d0 + timedelta(days=k)
            if rnd.random() < 0.07 or (day.weekday() >= 5 and rnd.random() < 0.9):
                continue
            logs.append(h.log(e["emp_code"], day.isoformat(), rnd.choice(["PRESENT"] * 8 + ["WFH", "ON_DUTY"]),
                              late=rnd.choice([0, 0, 0, 12, 25, 60]), ot=rnd.choice([0, 0, 35]), wh=round(rnd.uniform(3, 10), 2)))
    real_db.attendance_logs.insert_many(logs)
    m.ensure_indexes()
    c = TestClient(m.app)
    cases = [("/attendance", {}), ("/attendance", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
             ("/attendance", {"emp_code": "EMP0007"}), ("/attendance", {"status": "WFH"}),
             ("/analytics/employees/EMP0007/monthly", {"month": "2026-03"}),
             ("/analytics/departments/summary", {"month": "2026-03"}),
             ("/analytics/leaderboard/late", {"month": "2026-03"}),
             ("/analytics/departments/Sales/trend", {"from": "2026-03-01", "to": "2026-05-31"})]
    lines = [f"{len(logs)} attendance documents, {len(emps)} employees"]
    for url, params in cases:
        best = None
        for _ in range(3):
            t = time.perf_counter()
            assert c.get(url, params=params).status_code == 200
            best = min(best or 9e9, time.perf_counter() - t)
        lines.append(f"{url} {params}: best of 3 = {best * 1000:.0f} ms")
    for name, params in {"attendance_list": {"date_from": "2026-03-01", "date_to": "2026-03-31"},
                         "employee_monthly": {"emp_code": "EMP0007", "month": "2026-03"},
                         "department_summary": {"month": "2026-03"}, "late_leaderboard": {"month": "2026-03"},
                         "department_trend": {"department": "Sales", "from": "2026-03-01", "to": "2026-05-31"}}.items():
        r = c.get(f"/admin/explain/{name}", params=params)
        text = plan_text(r)
        stats = r.json()["explain"].get("executionStats", {})
        lines.append(f"explain {name}: IXSCAN={'IXSCAN' in text} COLLSCAN={'COLLSCAN' in text} "
                     f"docsExamined={stats.get('totalDocsExamined')} keysExamined={stats.get('totalKeysExamined')} "
                     f"millis={stats.get('executionTimeMillis')}")
        assert "IXSCAN" in text and "COLLSCAN" not in text, name
    with capsys.disabled():
        print("\n" + "\n".join(lines))
