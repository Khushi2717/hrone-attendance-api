"""Index creation: idempotent, never deletes data, works on a database that already holds records."""
from fastapi.testclient import TestClient

from app import main as m
from tests import helpers as h

EXPECTED = {
    "employees": {"uniq_emp_code", "dept_emp_code", "dept_joined_on", "joined_on"},
    "attendance_logs": {"uniq_emp_code_date", "date_desc_emp_code", "status_date_emp_code", "emp_code_punch_in"},
}


def names(db, coll):
    return set(db[coll].index_information()) - {"_id_"}


def test_indexes_created_over_existing_data_idempotently(backend_db_bare):
    db = backend_db_bare
    h.seed_july(db)                                    # the "grader" has already seeded the database
    before = {c: db[c].count_documents({}) for c in EXPECTED}
    assert names(db, "employees") == names(db, "attendance_logs") == set()   # only the default _id index
    m.ensure_indexes()
    m.ensure_indexes()                                 # second run must be a no-op, not an error
    m.ensure_indexes()
    assert {c: names(db, c) for c in EXPECTED} == EXPECTED
    assert {c: db[c].count_documents({}) for c in EXPECTED} == before          # nothing deleted or changed
    assert db.employees.index_information()["uniq_emp_code"].get("unique")
    assert db.attendance_logs.index_information()["uniq_emp_code_date"].get("unique")


def test_app_serves_requests_against_preseeded_database(backend_db_bare):
    h.seed_july(backend_db_bare)
    m.ensure_indexes()
    c = TestClient(m.app)
    assert c.get("/employees", params={"page_size": 100}).json()["total"] == 7
    assert c.get("/attendance", params={"emp_code": "EMP1001", "date_from": "2026-07-01", "date_to": "2026-07-31"}).json()["total"] == 6
    assert c.get("/attendance").json()["total"] == 13                           # exact, unfiltered total
    assert c.post("/employees", json={"emp_code": "EMP1001", "name": "x", "email": "a@b.co", "department": "d",
                                      "joined_on": "2026-01-01"}).status_code == 409   # duplicate of a seeded employee
    # punch-in for a seeded employee on a date that already has a seeded record -> 409; a new date -> 201
    day = m.ms_to_dt(int(m.datetime(2026, 7, 1, 9, 30, tzinfo=m.IST).timestamp()) * 1000)
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": m.dt_to_ms(day)}).status_code == 409
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP1003", "punched_at": m.dt_to_ms(day)}).status_code == 201


def test_startup_does_not_drop_data(backend_db_bare):
    h.seed_july(backend_db_bare)
    with TestClient(m.app):                            # runs the lifespan (index creation) like uvicorn does
        pass
    assert backend_db_bare.employees.count_documents({}) == 7
    assert backend_db_bare.attendance_logs.count_documents({}) == 13
