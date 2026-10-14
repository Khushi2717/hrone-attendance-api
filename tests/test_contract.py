"""Validates real API responses against the JSON Schemas inside openapi.yaml, and checks error shapes and that
nothing outside the contract (MongoDB _id, `id`, stray fields) leaks into a response."""
import copy
import pathlib

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app import main as m
from tests import helpers as h

SPEC = yaml.safe_load((pathlib.Path(__file__).parent.parent / "openapi.yaml").read_text())


def to_json_schema(node):
    """OpenAPI 3.0 -> JSON Schema: `nullable: true` becomes a type union."""
    if isinstance(node, dict):
        node = {k: to_json_schema(v) for k, v in node.items()}
        if node.get("nullable") and "type" in node:
            node["type"] = [node["type"], "null"]
        return node
    if isinstance(node, list):
        return [to_json_schema(v) for v in node]
    return node


COMPONENTS = to_json_schema(copy.deepcopy(SPEC["components"]))


def validate(name, instance):
    schema = {"components": COMPONENTS, "$ref": f"#/components/schemas/{name}"}
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, f"{name}: " + "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])


def declared_keys(name):
    return set(COMPONENTS["schemas"][name]["properties"])


@pytest.fixture()
def api(backend_db):
    h.seed_july(backend_db)
    return TestClient(m.app)


def ms(day, hour, minute):
    from datetime import datetime
    return int(datetime(2026, 7, day, hour, minute, tzinfo=m.IST).timestamp()) * 1000


def test_success_bodies_conform_to_schemas(api):
    r = api.post("/employees", json={"emp_code": "EMP7001", "name": "A", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-01"})
    assert r.status_code == 201
    validate("Employee", r.json())
    assert set(r.json()) == declared_keys("Employee")
    page = api.get("/employees").json()
    validate("EmployeePage", page)
    assert set(page) == declared_keys("EmployeePage") and all(set(i) == declared_keys("Employee") for i in page["items"])

    pin = api.post("/attendance/punch-in", json={"emp_code": "EMP7001", "punched_at": ms(6, 10, 5)})
    assert pin.status_code == 201
    validate("AttendanceRecord", pin.json())
    assert set(pin.json()) == declared_keys("AttendanceRecord")
    pout = api.post("/attendance/punch-out", json={"emp_code": "EMP7001", "punched_at": ms(6, 19, 10)})
    assert pout.status_code == 200
    validate("AttendanceRecord", pout.json())
    patched = api.patch("/attendance/EMP7001/2026-07-06", json={"punch_in": ms(6, 9, 28), "reason": "biometric glitch", "regularized_by": "hr"})
    assert patched.status_code == 200
    validate("AttendanceRecord", patched.json())
    entry = patched.json()["history"][0]
    assert set(entry) == declared_keys("HistoryEntry") and isinstance(entry["at"], int)
    assert all(isinstance(v["from"], (int, float, type(None))) for v in entry["changes"].values())

    listing = api.get("/attendance", params={"date_from": "2026-07-01", "date_to": "2026-07-31"}).json()
    validate("AttendancePage", listing)
    assert all(set(i) == declared_keys("AttendanceRecord") for i in listing["items"])

    validate("EmployeeMonthly", api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"}).json())
    assert set(api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"}).json()) == declared_keys("EmployeeMonthly")
    validate("EmployeeMonthly", api.get("/analytics/employees/EMP1004/monthly", params={"month": "2026-07"}).json())   # null pct
    summary = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    validate("DepartmentSummary", summary)
    assert all(set(i) == set(COMPONENTS["schemas"]["DepartmentSummary"]["properties"]["items"]["items"]["properties"]) for i in summary["items"])
    lb = api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()
    validate("Leaderboard", lb)
    trend = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-14"}).json()
    validate("Trend", trend)
    assert all(isinstance(i["headcount"], int) and isinstance(i["late_count"], int) for i in trend["items"])
    assert api.get("/health").status_code in (200, 503)


def test_error_bodies_have_the_documented_shape(api):
    for resp, code in [
        (api.get("/analytics/employees/EMP0000/monthly", params={"month": "2026-07"}), 404),
        (api.post("/attendance/punch-in", json={"emp_code": "NOPE"}), 404),
        (api.post("/employees", json={"emp_code": "EMP1001", "name": "A", "email": "a@x.io", "department": "D", "joined_on": "2026-01-01"}), 409),
        (api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}), 404),
    ]:
        assert resp.status_code == code and isinstance(resp.json()["detail"], str) and set(resp.json()) == {"detail"}
    for resp in [
        api.get("/attendance", params={"page": 0}),                                                             # pydantic 422
        api.get("/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-01"}),                    # rule 422
        api.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01", "to": "2027-07-01"}),     # rule 422
        api.patch("/attendance/EMP1001/2026-07-01", json={"reason": "valid reason", "regularized_by": "hr"}),  # "changes nothing"
    ]:
        assert resp.status_code == 422
        body = resp.json()
        assert set(body) == {"detail"} and isinstance(body["detail"], list) and all(isinstance(d, dict) and "msg" in d for d in body["detail"])
