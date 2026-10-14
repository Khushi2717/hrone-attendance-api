"""CRUD / rule / validation tests. Run on the `sim` backend (mongomock) and, when reachable, on a real MongoDB.

Covers request validation, status codes, serialisation and the attendance state machine."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main as m

IST_OFFSET_MS = 5.5 * 3600 * 1000


def ms(y, mo, d, h, mi, s=0):
    """IST wall clock -> epoch milliseconds."""
    dt = datetime(y, mo, d, h, mi, s, tzinfo=m.IST)
    return int(dt.timestamp()) * 1000


@pytest.fixture()
def client(backend_db):
    return TestClient(m.app)


def make_emp(client, code="EMP0001", **over):
    body = {"emp_code": code, "name": "Asha", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-05", **over}
    return client.post("/employees", json=body)


def test_employee_create_shape_and_duplicate(client):
    r = make_emp(client)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert body["shift_start"] == "09:30" and isinstance(body["created_at"], int) and body["created_at"] > 10**11
    assert make_emp(client).status_code == 409


@pytest.mark.parametrize("over", [{"emp_code": "E1"}, {"emp_code": "EMP12"}, {"email": "nope"}, {"joined_on": "2026-02-30"},
                                  {"shift_start": "9:30"}, {"shift_start": "10:00", "shift_end": "10:00"}, {"name": ""}])
def test_employee_validation_422(client, over):
    assert make_emp(client, **over).status_code == 422


def test_employee_list_filter_sort_pagination_total(client):
    for i, dept in [(3, "A"), (1, "B"), (2, "A"), (4, "A")]:
        make_emp(client, f"EMP000{i}", department=dept)
    r = client.get("/employees", params={"department": "A", "page": 1, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0002", "EMP0003"] and r["total"] == 3
    r2 = client.get("/employees", params={"department": "A", "page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in r2["items"]] == ["EMP0004"]
    assert client.get("/employees", params={"department": "A", "page": 9}).json()["items"] == []
    assert client.get("/employees", params={"department": "none"}).json()["total"] == 0


def test_punch_in_late_boundaries_and_duplicate(client):
    make_emp(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 0) + 900})
    assert r.status_code == 201
    rec = r.json()
    assert rec["date"] == "2026-07-06" and rec["late_minutes"] == 0 and rec["punch_in"] == ms(2026, 7, 6, 9, 40, 0)
    assert rec["punch_out"] is None and rec["work_hours"] is None and rec["history"] == [] and rec["half_day"] is False
    assert "id" not in rec and "_id" not in rec
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 11, 0)}).status_code == 409
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 40, 1)})
    assert r.json()["late_minutes"] == 10


def test_punch_in_errors(client):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    make_emp(client)
    for bad in ({"punched_at": 1_783_312_500}, {"punched_at": 1.7e12}, {"punched_at": "1783312500000"},
                {"status": "ABSENT"}, {"status": "LEAVE"}):
        assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", **bad}).status_code == 422


def test_punch_in_status_variants_and_default_now(client):
    make_emp(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "WFH"})
    assert r.status_code == 201 and r.json()["status"] == "WFH"


def test_overnight_punch_in_date(client):
    make_emp(client, "EMP0005", shift_start="22:00", shift_end="06:00")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 5, 30)})
    assert r.json()["date"] == "2026-07-06"


def test_punch_out_flow_overtime_halfday_and_errors(client):
    make_emp(client)
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404  # nothing to close
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 6)}).status_code == 422
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10)})
    assert r.status_code == 200
    body = r.json()
    assert (body["work_hours"], body["late_minutes"], body["overtime_minutes"], body["half_day"]) == (9.08, 35, 40, False)
    assert body["history"] == []
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 20, 0)}).status_code == 409
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP404"}).status_code == 404


def test_punch_out_overtime_threshold(client):
    make_emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 59, 59)})
    assert r.json()["overtime_minutes"] == 0
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 19, 0)})
    assert r.json()["overtime_minutes"] == 30


def test_punch_out_across_midnight(client):
    make_emp(client, "EMP0005", shift_start="22:00", shift_end="06:00")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200
    b = r.json()
    assert (b["date"], b["work_hours"], b["overtime_minutes"], b["late_minutes"]) == ("2026-07-06", 8.75, 40, 0)


def test_half_day_rounding_boundary(client):
    make_emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0) + 16182 * 1000})
    assert r.json()["work_hours"] == 4.5 and r.json()["half_day"] is False


def test_attendance_list_filters_sort_total(client):
    make_emp(client, "EMP0001")
    make_emp(client, "EMP0002")
    for code in ("EMP0001", "EMP0002"):
        for day in (6, 7, 8):
            client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026, 7, day, 9, 30)})
    r = client.get("/attendance", params={"page_size": 4}).json()
    assert r["total"] == 6
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002")]
    r = client.get("/attendance", params={"emp_code": "EMP0002", "date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 1 and r["items"][0]["date"] == "2026-07-07"
    assert client.get("/attendance", params={"status": "LEAVE"}).json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
    for bad in ({"status": "BOGUS"}, {"date_from": "2026-07-09", "date_to": "2026-07-01"}, {"date_from": "x"},
                {"page": 0}, {"page_size": 0}, {"page_size": 101}):
        assert client.get("/attendance", params=bad).status_code == 422


def seed_record(code="EMP0001", day="2026-07-07", **over):
    rec = {"emp_code": code, "date": day, "status": "PRESENT",
           "punch_in": datetime(2026, 7, 7, 4, 35, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc),
           "work_hours": 8.42, "late_minutes": 35, "overtime_minutes": 0}  # legacy: no half_day, no history
    rec.update(over)
    return rec


def test_regularize_recomputes_and_appends_history(client):
    make_emp(client)
    m.db.attendance_logs.insert_one(seed_record())
    r = client.patch("/attendance/EMP0001/2026-07-07",
                     json={"punch_in": ms(2026, 7, 7, 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin"})
    assert r.status_code == 200
    b = r.json()
    assert (b["late_minutes"], b["work_hours"], b["overtime_minutes"]) == (0, 9.03, 0)
    assert len(b["history"]) == 1
    h = b["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch"
    assert h["changes"] == {"punch_in": {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)},
                            "work_hours": {"from": 8.42, "to": 9.03}, "late_minutes": {"from": 35, "to": 0}}
    # second correction appends, never overwrites
    r2 = client.patch("/attendance/EMP0001/2026-07-07",
                      json={"punch_out": ms(2026, 7, 7, 19, 10), "reason": "forgot to punch", "regularized_by": "hr2"})
    hist = r2.json()["history"]
    assert len(hist) == 2 and hist[0] == h and set(hist[1]["changes"]) == {"punch_out", "work_hours", "overtime_minutes"}
    assert "late_minutes" not in hist[1]["changes"]  # unchanged fields are not recorded


def test_regularize_rules(client):
    make_emp(client)
    m.db.attendance_logs.insert_one(seed_record())
    base = {"reason": "valid reason", "regularized_by": "hr"}
    url = "/attendance/EMP0001/2026-07-07"
    assert client.patch(url, json={**base}).status_code == 422                                    # nothing changes
    assert client.patch(url, json={**base, "status": "PRESENT"}).status_code == 422               # same status
    assert client.patch(url, json={**base, "status": "LEAVE", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422
    assert client.patch(url, json={**base, "punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422  # leaves attendance date
    assert client.patch(url, json={**base, "punch_out": ms(2026, 7, 7, 10, 0)}).status_code == 422  # punch_out <= punch_in (10:05)
    assert client.patch(url, json={**base, "punch_in": None}).status_code == 422
    assert client.patch(url, json={"reason": "abc", "regularized_by": "hr", "status": "LEAVE"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
    assert client.patch("/attendance/EMP0404/2026-07-07", json={**base, "status": "LEAVE"}).status_code == 404
    # derived fields in the body are ignored, not applied
    r = client.patch(url, json={**base, "status": "WFH", "late_minutes": 99, "work_hours": 1.0})
    assert r.status_code == 200 and r.json()["late_minutes"] == 35 and r.json()["work_hours"] == 8.42


def test_regularize_to_absent_clears_times_and_derived(client):
    make_emp(client)
    m.db.attendance_logs.insert_one(seed_record())
    r = client.patch("/attendance/EMP0001/2026-07-07", json={"status": "ABSENT", "reason": "wrong record", "regularized_by": "hr"})
    b = r.json()
    assert (b["status"], b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"]) == ("ABSENT", None, None, None, 0)
    assert set(b["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}
    # back to a presence status needs a punch_in
    assert client.patch("/attendance/EMP0001/2026-07-07", json={"status": "PRESENT", "reason": "restore it", "regularized_by": "hr"}).status_code == 422
    ok = client.patch("/attendance/EMP0001/2026-07-07", json={"status": "PRESENT", "punch_in": ms(2026, 7, 7, 9, 30), "reason": "restore it", "regularized_by": "hr"})
    assert ok.status_code == 200 and ok.json()["late_minutes"] == 0 and len(ok.json()["history"]) == 2


def test_legacy_record_defaults_in_list(client):
    make_emp(client)
    m.db.attendance_logs.insert_one(seed_record())
    item = client.get("/attendance").json()["items"][0]
    assert item["half_day"] is False and item["history"] == [] and item["overtime_minutes"] == 0
    assert "_id" not in item and "id" not in item


def test_health_ok_with_working_db(client, monkeypatch):
    class Admin:
        def command(self, *_a, **_k):
            return {"ok": 1}
    monkeypatch.setattr(m, "client", type("C", (), {"admin": Admin()})())
    monkeypatch.setitem(m.app_state, "indexes_ready", True)
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# ---------------------------------------------------------------- additional edge cases
def test_epoch_bounds(client):
    make_emp(client)
    post = lambda v: client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": v}).status_code  # noqa: E731
    assert post(99_999_999_999) == 422 and post(4_102_444_800_001) == 422 and post(-1) == 422 and post(0) == 422
    assert post(True) == 422 and post(None) == 201          # null/omitted means "now"
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 4_102_444_800_000})
    assert r.status_code == 201                              # upper bound is inclusive


def test_midnight_ist_attendance_date(client):
    make_emp(client)
    last_second = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 23, 59, 59) + 999})
    assert last_second.json()["date"] == "2026-07-06" and last_second.json()["punch_in"] == ms(2026, 7, 6, 23, 59, 59)
    first_second = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 0, 0, 0)})
    assert first_second.json()["date"] == "2026-07-07"      # one second later is the next IST date, whatever UTC says
    assert first_second.json()["late_minutes"] == 0         # 00:00 is before the 09:30 shift start: not late


def test_punch_out_24_hour_limit_and_equal_instant(client):
    make_emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    out = lambda t: client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t})  # noqa: E731
    assert out(ms(2026, 7, 6, 9, 30)).status_code == 422                       # equal to punch_in
    assert out(ms(2026, 7, 7, 9, 30, 1)).status_code == 422                    # 24h and 1s
    assert out(ms(2026, 7, 6, 9, 29)).status_code == 404                       # before any punch-in: nothing to close
    r = out(ms(2026, 7, 7, 9, 30))                                             # exactly 24h is allowed
    assert r.status_code == 200 and r.json()["work_hours"] == 24.0 and r.json()["half_day"] is False


def test_punch_out_picks_most_recent_record_and_409_for_closed_one(client):
    make_emp(client)
    for day in (6, 7):
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, day, 9, 30)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    # most recent record is closed -> 409, the older still-open record is NOT silently closed instead
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 19, 0)}).status_code == 409
    assert client.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-06", "date_to": "2026-07-06"}).json()["items"][0]["punch_out"] is None


def test_punch_out_ignores_absent_and_leave_records(client):
    make_emp(client)
    m.db.attendance_logs.insert_one(seed_record(status="LEAVE", punch_in=None, punch_out=None, work_hours=None, late_minutes=0))
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 0)}).status_code == 404


def test_regularize_overnight_record(client):
    make_emp(client, "EMP0005", shift_start="22:00", shift_end="06:00")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 22, 30)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 45)})
    url = "/attendance/EMP0005/2026-07-06"
    base = {"reason": "valid reason", "regularized_by": "hr"}
    r = client.patch(url, json={**base, "punch_in": ms(2026, 7, 6, 21, 55)})
    assert r.status_code == 200 and (r.json()["late_minutes"], r.json()["overtime_minutes"], r.json()["work_hours"]) == (0, 45, 8.83)
    # a punch_in on 07-07 05:00 still belongs to the 07-06 shift (before shift_end 06:00): allowed
    assert client.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 5, 0), "punch_out": ms(2026, 7, 7, 6, 45)}).status_code == 200
    # 07-07 09:00 is the next shift's date: rejected
    assert client.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 9, 0), "punch_out": ms(2026, 7, 7, 15, 0)}).status_code == 422


def test_regularize_punch_out_limits(client):
    make_emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    url, base = "/attendance/EMP0001/2026-07-06", {"reason": "valid reason", "regularized_by": "hr"}
    assert client.patch(url, json={**base, "punch_out": ms(2026, 7, 6, 9, 30)}).status_code == 422
    assert client.patch(url, json={**base, "punch_out": ms(2026, 7, 7, 9, 30, 1)}).status_code == 422
    assert client.patch(url, json={**base, "punch_out": ms(2026, 7, 7, 9, 30)}).status_code == 200


def test_regularize_validation_422s(client):
    make_emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    url = "/attendance/EMP0001/2026-07-06"
    ok = {"status": "WFH", "reason": "valid reason", "regularized_by": "hr"}
    for bad in ({"reason": "x" * 201}, {"regularized_by": ""}, {"regularized_by": "x" * 51}, {"status": "BOGUS"},
                {"punch_out": 1.5e12}, {"punch_in": "1783312500000"}, {"punch_out": 1_783_312_500}):
        assert client.patch(url, json={**ok, **bad}).status_code == 422, bad
    assert client.patch(url, json={"status": "WFH"}).status_code == 422                  # reason / regularized_by required
    assert client.patch("/attendance/EMP0001/2026-7-6", json=ok).status_code == 422     # malformed date in the path
    assert client.patch(url, content=b"not json", headers={"content-type": "application/json"}).status_code == 422


def test_regularize_midnight_boundary_punch_in(client):
    make_emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    url, base = "/attendance/EMP0001/2026-07-06", {"reason": "valid reason", "regularized_by": "hr"}
    assert client.patch(url, json={**base, "punch_in": ms(2026, 7, 6, 23, 59, 59)}).status_code == 200
    assert client.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 0, 0, 0)}).status_code == 422   # next IST date


def test_pagination_and_exact_totals(client):
    for i in range(1, 8):
        make_emp(client, f"EMP000{i}", department="A" if i % 2 else "B")
    for i in range(1, 8):
        client.post("/attendance/punch-in", json={"emp_code": f"EMP000{i}", "punched_at": ms(2026, 7, 6, 9, 30)})
    assert client.get("/attendance").json()["total"] == 7                     # unfiltered total is exact
    page = lambda n, size: client.get("/attendance", params={"page": n, "page_size": size}).json()  # noqa: E731
    assert [i["emp_code"] for i in page(3, 3)["items"]] == ["EMP0007"] and page(3, 3)["total"] == 7
    assert page(4, 3)["items"] == [] and page(4, 3)["total"] == 7
    assert page(1, 100)["page_size"] == 100
    assert client.get("/employees", params={"department": ""}).json()["total"] == 0   # empty string is a filter, not "no filter"
    assert client.get("/attendance", params={"emp_code": ""}).json()["total"] == 0
    assert client.get("/attendance", params={"page": 10 ** 30}).json()["items"] == []  # absurd page: empty, not a 500


def test_noop_patch_is_422_even_when_stored_derived_values_disagree_with_a_recomputation(client):
    """Seeded data may hold derived values that differ from what R2-R5 would compute; a request that edits
    nothing must neither succeed nor touch the record."""
    make_emp(client)
    m.db.attendance_logs.insert_one(seed_record(late_minutes=99, work_hours=1.23, overtime_minutes=7, half_day=True))
    before = m.db.attendance_logs.find_one({"emp_code": "EMP0001"}, {"_id": 0})
    base = {"reason": "valid reason", "regularized_by": "hr"}
    url = "/attendance/EMP0001/2026-07-07"
    assert client.patch(url, json=base).status_code == 422
    assert client.patch(url, json={**base, "status": "PRESENT"}).status_code == 422
    assert client.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 10, 5)}).status_code == 422   # same instant as stored
    assert m.db.attendance_logs.find_one({"emp_code": "EMP0001"}, {"_id": 0}) == before
    # a real edit does recompute (and records) every derived field that differs from what was stored
    r = client.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 9, 28)})
    assert r.status_code == 200 and r.json()["late_minutes"] == 0 and r.json()["half_day"] is False
    assert {"punch_in", "late_minutes", "work_hours", "overtime_minutes", "half_day"} <= set(r.json()["history"][0]["changes"])
