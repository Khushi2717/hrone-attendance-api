"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is read, but real environment variables win)

Contract: openapi.yaml.  Stored data: DATA_MODEL.md.  Business rules R1-R10 are referenced inline.

Layout of this single file
    1. configuration + database handle
    2. time / rounding helpers (the only place that touches time zones)
    3. request models and validation helpers
    4. attendance rules (R1-R5) and serialisation
    5. indexes + startup
    6. endpoints: system, employees, attendance
    7. aggregation pipeline builders + analytics endpoints
    8. explain endpoint
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False: real environment variables take priority over .env

log = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns aware UTC datetimes instead of naive ones, so a stored
# instant can never be mistaken for host-local time. All instants in this file are UTC.
client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=30000,
    maxPoolSize=100,
)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60                 # R2
OVERTIME_MIN_MINUTES = 30               # R3
HALF_DAY_BELOW = Decimal("4.50")        # R5
MAX_PUNCH_SPAN_SECONDS = 24 * 3600      # punch-out / regularize: at most 24h after punch-in
MAX_TREND_DAYS = 92
MIN_EPOCH_MS = 100_000_000_000          # below this it "looks like seconds"
MAX_EPOCH_MS = 4_102_444_800_000
REGULARIZE_MAX_ATTEMPTS = 5
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]   # R6
HALF_DAY_WEIGHT = 0.5                   # R5
# Added before flooring so a rate that is exactly on a .5 tie is not pushed down by
# binary floating point error (see half_up_4_expr).
HALF_UP_EPSILON = 1e-9

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

app_state = {"indexes_ready": False}


# --------------------------------------------------------------------------- #
# 2. Time and rounding helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> epoch milliseconds using integer arithmetic only (no float rounding)."""
    if dt.tzinfo is None:  # defensive: anything naive coming from the DB is UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_ms_precision() -> datetime:
    """Server clock at millisecond precision (BSON dates cannot hold more)."""
    return EPOCH + timedelta(milliseconds=time.time_ns() // 1_000_000)


def now_seconds_precision() -> datetime:
    return datetime.fromtimestamp(time.time_ns() // 1_000_000_000, tz=UTC)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def round_half_up(value: Any, places: int) -> Decimal:
    """Decimal half-up rounding (R4, R8). Python's round() is half-even and works on binary floats."""
    quantum = Decimal(1).scaleb(-places)
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return dec.quantize(quantum, rounding=ROUND_HALF_UP)


def hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def ist_instant(day: date, clock: str) -> datetime:
    hour, minute = hhmm(clock)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def count_weekdays(first: date, last: date) -> int:
    """Mon-Fri days in [first, last] (R7). At most 31 iterations; no holiday calendar."""
    total, day = 0, first
    while day <= last:
        if day.weekday() < 5:
            total += 1
        day += timedelta(days=1)
    return total


# --------------------------------------------------------------------------- #
# 3. Request models and validation helpers
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# StrictInt: JSON floats ("1.5", 1.0), strings and booleans are rejected with 422.
EpochMs = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
ShiftTime = Annotated[str, Field(pattern=HHMM_PATTERN)]


def unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def parse_date(value: str, name: str = "date") -> date:
    if not DATE_RE.match(value):
        raise unprocessable(f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"{name} is not a valid calendar date") from None


def parse_month(value: str) -> tuple[date, date]:
    """YYYY-MM -> (first day, last day)."""
    if not re.match(MONTH_PATTERN, value):
        raise unprocessable("month must be YYYY-MM")
    year, month = int(value[:4]), int(value[5:])
    if year < 1:
        raise unprocessable("month is out of range")
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, value: str) -> str:
        date.fromisoformat(value)  # ValueError -> 422
        return value

    @model_validator(mode="after")
    def shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    # Derived fields are not declared, so a client that sends them has them ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def optional_fields_are_not_null(self) -> "RegularizeRequest":
        # The contract makes these optional but not nullable: explicit null is invalid.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


# --------------------------------------------------------------------------- #
# 4. Attendance rules (R1-R5) and serialisation
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; an overnight-shift punch earlier than shift_end belongs
    to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and (local.hour, local.minute, local.second) < (*hhmm(shift_end), 0):
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, day: date, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole minutes since shift start."""
    elapsed = int((punch_in - ist_instant(day, shift_start)).total_seconds())
    return elapsed // 60 if elapsed > GRACE_SECONDS else 0


def shift_end_instant(day: date, shift_start: str, shift_end: str) -> datetime:
    end = ist_instant(day, shift_end)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_overtime_minutes(punch_out: datetime, end: datetime) -> int:
    """R3: whole minutes after shift end, counted only if at least 30."""
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(round_half_up(Decimal(seconds) / Decimal(3600), 2))


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  day: date, emp: dict) -> dict:
    """All four derived fields from the final state (DATA_MODEL.md section 3 table)."""
    derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENT_STATUSES or punch_in is None:
        return derived
    derived["late_minutes"] = compute_late_minutes(punch_in, day, emp["shift_start"])
    if punch_out is not None:
        work_hours = compute_work_hours(punch_in, punch_out)
        derived["work_hours"] = work_hours
        derived["overtime_minutes"] = compute_overtime_minutes(
            punch_out, shift_end_instant(day, emp["shift_start"], emp["shift_end"]))
        derived["half_day"] = Decimal(str(work_hours)) < HALF_DAY_BELOW  # R5 uses the rounded value
    return derived


def json_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def record_out(rec: dict) -> dict:
    """Stored attendance document -> API shape. Missing legacy fields get their defaults."""
    history = []
    for entry in rec.get("history") or []:
        changes = {key: {"from": json_value(ch.get("from")), "to": json_value(ch.get("to"))}
                   for key, ch in (entry.get("changes") or {}).items()}
        history.append({"at": dt_to_ms(entry["at"]), "by": entry.get("by"),
                        "reason": entry.get("reason"), "changes": changes})
    work_hours = rec.get("work_hours")
    return {
        "emp_code": rec["emp_code"],
        "date": rec["date"],
        "status": rec["status"],
        "punch_in": json_value(rec.get("punch_in")),
        "punch_out": json_value(rec.get("punch_out")),
        "work_hours": None if work_hours is None else float(work_hours),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
        "history": history,
    }


def employee_out(emp: dict) -> dict:
    return {
        "emp_code": emp["emp_code"], "name": emp["name"], "email": emp["email"],
        "department": emp["department"], "shift_start": emp["shift_start"],
        "shift_end": emp["shift_end"], "joined_on": emp["joined_on"],
        "created_at": dt_to_ms(emp["created_at"]),
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def count_matching(collection, flt: dict) -> int:
    # An empty filter is answered from collection metadata instead of scanning ~100k documents.
    return collection.estimated_document_count() if not flt else collection.count_documents(flt)


# --------------------------------------------------------------------------- #
# 5. Indexes and startup
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Idempotent (create_index with an identical spec is a no-op). Never drops or deletes anything."""
    # employees
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # GET /employees?department=..  (sorted by emp_code); department lookups in analytics
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # trend headcount per day + department summary with a department filter (joined_on <= day)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    # department summary across all departments (joined_on <= end of month)
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # attendance_logs
    # one record per (employee, day): punch-in race guard, monthly analytics, per-employee listing
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="uniq_emp_code_date")
    # GET /attendance default sort (date desc, emp_code asc) + date-range scans for monthly analytics
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    # GET /attendance?status=..  with the same sort
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
                                    name="status_date_emp_code")
    # punch-out: "most recent record of this employee with punch_in <= punched_at"
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_code_punch_in")


def try_ensure_indexes() -> bool:
    try:
        ensure_indexes()
        app_state["indexes_ready"] = True
    except PyMongoError:
        # e.g. database not reachable yet, or seeded duplicates blocking a unique index.
        log.exception("index creation failed")
        app_state["indexes_ready"] = False
    return app_state["indexes_ready"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    try_ensure_indexes()  # a failure is logged, and /health retries; the process still starts
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def database_unavailable(_: Request, exc: ConnectionFailure) -> JSONResponse:
    log.error("database unavailable: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


@app.exception_handler(PyMongoError)
async def database_error(_: Request, exc: PyMongoError) -> JSONResponse:
    log.error("database error: %s", type(exc).__name__)  # no connection details in logs or responses
    return JSONResponse(status_code=500, content={"detail": "internal database error"})


# --------------------------------------------------------------------------- #
# 6. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    try:
        client.admin.command("ping")  # bounded by serverSelectionTimeoutMS
    except PyMongoError:
        raise HTTPException(503, "database unavailable") from None
    if not app_state["indexes_ready"]:
        try_ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict:
    doc = body.model_dump()
    doc["created_at"] = now_ms_precision()
    try:
        # Uniqueness is enforced by the unique index, so concurrent duplicates yield one 201 and 409s.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    flt = {} if department is None else {"department": department}
    total = count_matching(db.employees, flt)
    skip = (page - 1) * page_size
    items: list = []
    if skip < total:
        cursor = db.employees.find(flt, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [employee_out(e) for e in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict:
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds_precision()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # No read-then-insert: the unique (emp_code, date) index decides the race atomically.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return record_out(doc)


def build_attendance_filter(emp_code: Optional[str], date_from: Optional[str],
                            date_to: Optional[str], status: Optional[str]) -> dict:
    """Shared by GET /attendance and GET /admin/explain/attendance_list."""
    start = parse_date(date_from, "date_from") if date_from is not None else None
    end = parse_date(date_to, "date_to") if date_to is not None else None
    if start and end and start > end:
        raise unprocessable("date_from must not be after date_to")
    flt: dict = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if start or end:
        flt["date"] = {}
        if start:
            flt["date"]["$gte"] = start.isoformat()
        if end:
            flt["date"]["$lte"] = end.isoformat()
    if status is not None:
        flt["status"] = status
    return flt


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    flt = build_attendance_filter(emp_code, date_from, date_to, status)
    total = count_matching(db.attendance_logs, flt)
    skip = (page - 1) * page_size
    items: list = []
    if skip < total:
        cursor = db.attendance_logs.find(flt, {"_id": 0}).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)
        items = [record_out(r) for r in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict:
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds_precision()
    # Most recent record with punch_in <= punched_at. This also finds an overnight record that
    # started the previous day. Records with a null punch_in (ABSENT/LEAVE) never match a $lte.
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                                      sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_dt = as_utc(rec["punch_in"])
    span = int((ts - punch_in_dt).total_seconds())
    if span <= 0:
        raise unprocessable("punched_at must be after punch_in")
    if span > MAX_PUNCH_SPAN_SECONDS:
        raise unprocessable("punched_at must be within 24 hours of punch_in")
    derived = derive_fields(rec["status"], punch_in_dt, ts, date.fromisoformat(rec["date"]), emp)
    # Atomic state transition: only the request that still sees punch_out == null (and the same
    # punch_in/status) can win. Every other concurrent punch-out matches nothing -> 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": ts, "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out or record changed concurrently")
    return record_out(updated)


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def stored_state(rec: dict) -> dict:
    """The tracked fields of a stored record with legacy defaults applied and instants as aware UTC."""
    return {
        "status": rec["status"],
        "punch_in": as_utc(rec.get("punch_in")),
        "punch_out": as_utc(rec.get("punch_out")),
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }


def plan_regularization(rec: dict, emp: dict, body: RegularizeRequest) -> tuple[dict, dict]:
    """Apply the request to the stored record in memory. Returns (new tracked state, changes)."""
    day = date.fromisoformat(rec["date"])
    old = stored_state(rec)
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None
    status = body.status or old["status"]

    if status not in PRESENT_STATUSES:
        if new_in is not None or new_out is not None:
            raise unprocessable("status ABSENT/LEAVE clears punch times; do not supply punch_in or punch_out")
        final_in = final_out = None
    else:
        final_in = new_in if new_in is not None else old["punch_in"]
        if final_in is None:
            raise unprocessable("a presence status requires a punch_in")
        final_out = new_out if new_out is not None else old["punch_out"]
        if new_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            raise unprocessable("punch_in must stay on the record's attendance date")
        if final_out is not None and (new_in is not None or new_out is not None):
            span = int((final_out - final_in).total_seconds())
            if span <= 0 or span > MAX_PUNCH_SPAN_SECONDS:
                raise unprocessable("punch_out must be after punch_in and within 24 hours of it")

    new = {"status": status, "punch_in": final_in, "punch_out": final_out,
           **derive_fields(status, final_in, final_out, day, emp)}
    changes = {k: {"from": old[k], "to": new[k]} for k in TRACKED_FIELDS if old[k] != new[k]}
    if not changes:
        raise unprocessable("request changes nothing")
    return new, changes


def history_length_filter(rec: dict) -> dict:
    n = len(rec.get("history") or [])
    if n == 0:  # legacy documents may not have the field at all
        return {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    return {"history": {"$size": n}}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeRequest, emp_code: str,
               day_str: str = Path(alias="date")) -> dict:
    parse_date(day_str)
    emp = get_employee_or_404(emp_code)
    for _ in range(REGULARIZE_MAX_ATTEMPTS):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day_str})
        if rec is None:
            raise HTTPException(404, "no attendance record for this date")
        new, changes = plan_regularization(rec, emp, body)
        entry = {"at": now_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # Optimistic concurrency: the write only applies if the record is still exactly what we read
        # (same status/punch times, same history length). If another writer got there first nothing
        # matches, and we re-read and recompute on top of their change, so no history entry is lost.
        flt = {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"),
               "punch_out": rec.get("punch_out"), **history_length_filter(rec)}
        updated = db.attendance_logs.find_one_and_update(
            flt, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record is being modified concurrently; retry")


# --------------------------------------------------------------------------- #
# 7. Aggregation pipelines and analytics endpoints
# --------------------------------------------------------------------------- #
# Expressions over a stored attendance document. Derived values are READ, never recomputed (DATA_MODEL.md).
IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
# date strings are plain calendar dates; $dayOfWeek on a UTC-parsed date gives 1=Sun ... 7=Sat.
DAY_OF_WEEK = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None}}}
IS_WEEKDAY = {"$in": [DAY_OF_WEEK, [2, 3, 4, 5, 6]]}
PRESENCE_WEIGHT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, HALF_DAY_WEIGHT, 1]}
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE_MINUTES, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def record_metrics() -> dict:
    """$group accumulators shared by the employee monthly and department summary pipelines."""
    return {
        "present_days": {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, PRESENCE_WEIGHT, 0]}},   # R5-R7
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
        "total_late_minutes": {"$sum": LATE_MINUTES},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # work hours as integer hundredths so the sum is exact; divided and rounded half-up in Python
        "work_hundredths": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
        "work_records": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
    }


def month_bounds(month: str) -> tuple[str, str, date, date]:
    first, last = parse_month(month)
    return first.isoformat(), last.isoformat(), first, last


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None, **record_metrics()}},
    ]


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    employee_match: dict = {"joined_on": {"$lte": end}}  # R9: joined on or before the end of the month
    if department is not None:
        employee_match["department"] = department
    return [
        # Start from EMPLOYEES so people with zero logs still produce a row (and count in headcount).
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}},
                         {"$group": {"_id": None, **record_metrics()}}],
            "as": "agg"}},
        # one row per employee: preserveNullAndEmptyArrays keeps employees whose lookup is empty
        {"$unwind": {"path": "$agg", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$agg.present_days"},
            "late_count": {"$sum": "$agg.late_count"},
            "total_late_minutes": {"$sum": "$agg.total_late_minutes"},
            "leave_count": {"$sum": "$agg.leave_count"},
            "on_duty_count": {"$sum": "$agg.on_duty_count"},
            "work_hundredths": {"$sum": "$agg.work_hundredths"},
            "work_records": {"$sum": "$agg.work_records"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},   # limit AFTER ranking: a tie at the cutoff keeps all tied rows
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def half_up_4_expr(ratio: dict) -> dict:
    """ratio -> integer ten-thousandths, rounded half-up (R8). $round is half-even, so floor(x*10000 + 0.5)."""
    return {"$floor": {"$add": [{"$multiply": [ratio, 10000]}, 0.5 + HALF_UP_EPSILON]}}


def department_trend_pipeline(department: str, first: date, last: date) -> list:
    days = (last - first).days + 1
    first_dt = datetime(first.year, first.month, first.day, tzinfo=UTC)
    from_s, to_s = first.isoformat(), last.isoformat()
    window = {"documents": [-6, 0]}  # current row + up to 6 preceding rows inside the requested range
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": from_s, "$lte": to_s}}},
                {"$project": {"_id": 0, "date": 1,
                              "present": {"$cond": [IS_PRESENT, PRESENCE_WEIGHT, 0]},
                              "late": {"$cond": [IS_LATE, 1, 0]}}}],
            "as": "logs"}},
        {"$unwind": "$logs"},
        {"$group": {"_id": "$logs.date", "present_count": {"$sum": "$logs.present"},
                    "late_count": {"$sum": "$logs.late"}}},
        # Gap filling inside the database: one zero-row for every calendar day of the range is generated
        # with $range and unioned in; the following $group merges it with the real per-day rows.
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"_id": 0, "offset": {"$range": [0, days]}}},
            {"$unwind": "$offset"},
            {"$project": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": {
                    "$add": [first_dt, {"$multiply": ["$offset", 86_400_000]}]}}},
                "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}}]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        # R9: headcount = department employees with joined_on <= that day
        {"$lookup": {"from": "employees", "let": {"day": "$_id"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                  "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}}},
        {"$set": {"is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}}},
        {"$set": {"rate4": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_4_expr({"$divide": ["$present_count", "$headcount"]}), None]}}},
        {"$set": {"has_rate": {"$cond": [{"$eq": ["$rate4", None]}, 0, 1]}}},
        # Moving average = mean of the (rounded) non-null rates in the window; kept as sum and count
        # of integer ten-thousandths so the final division is exact in Decimal.
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {
            "window_sum": {"$sum": "$rate4", "window": window},
            "window_n": {"$sum": "$has_rate", "window": window}}}},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "date": "$_id", "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "rate4": 1, "window_sum": 1, "window_n": 1}},
    ]


def money2(value: Any) -> float:
    return float(round_half_up(value, 2))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_PATTERN)) -> dict:
    start, end, first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    m = rows[0] if rows else {}
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last) if joined <= last else 0  # R7
    present = Decimal(str(m.get("present_days", 0)))
    pct = None if working_days == 0 else money2(present / Decimal(working_days) * 100)
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": money2(present), "leave_days": int(m.get("leave_count", 0)),
        "late_count": int(m.get("late_count", 0)), "total_late_minutes": int(m.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(m.get("total_overtime_minutes", 0)), "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_PATTERN), department: Optional[str] = None) -> dict:
    start, end, _, _ = month_bounds(month)
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(start, end, department)):
        records = int(r["work_records"])
        avg = None if records == 0 else money2(Decimal(int(r["work_hundredths"])) / Decimal(records) / 100)
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]), "present_days": money2(r["present_days"]),
            "avg_work_hours": avg, "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(pattern=MONTH_PATTERN), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None) -> dict:
    start, end, _, _ = month_bounds(month)
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
              "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
             for r in db.attendance_logs.aggregate(late_leaderboard_pipeline(start, end, limit, department))]
    return {"month": month, "items": items}


def parse_trend_range(from_: str, to: str) -> tuple[date, date]:
    first, last = parse_date(from_, "from"), parse_date(to, "to")
    if last < first:
        raise unprocessable("to must not be before from")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"range must not exceed {MAX_TREND_DAYS} days")
    return first, last


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()) -> dict:
    first, last = parse_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = []
    for r in db.employees.aggregate(department_trend_pipeline(department, first, last)):
        rate = None if r.get("rate4") is None else float(Decimal(int(r["rate4"])) / Decimal(10000))
        n = int(r["window_n"])
        moving = None if n == 0 else float(round_half_up(
            Decimal(int(r["window_sum"])) / Decimal(n) / Decimal(10000), 4))
        items.append({"date": r["date"], "is_working_day": bool(r["is_working_day"]),
                      "headcount": int(r["headcount"]), "present_count": money2(r["present_count"]),
                      "late_count": int(r["late_count"]), "attendance_rate": rate, "moving_avg_7d": moving})
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


def require(value: Any, name: str) -> Any:
    if value is None:
        raise unprocessable(f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    """Runs MongoDB explain (executionStats) on the same query / pipeline the real endpoint builds."""
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {"find": collection,
                   "filter": build_attendance_filter(emp_code, date_from, date_to, status),
                   "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        collection = "attendance_logs"
        start, end, _, _ = month_bounds(require(month, "month"))
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": employee_monthly_pipeline(require(emp_code, "emp_code"), start, end)}
    elif endpoint == "department_summary":
        collection = "employees"
        start, end, _, _ = month_bounds(require(month, "month"))
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": department_summary_pipeline(start, end, department)}
    elif endpoint == "late_leaderboard":
        collection = "attendance_logs"
        start, end, _, _ = month_bounds(require(month, "month"))
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": late_leaderboard_pipeline(start, end, limit, department)}
    else:  # department_trend
        collection = "employees"
        first, last = parse_trend_range(require(from_, "from"), require(to, "to"))
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": department_trend_pipeline(require(department, "department"), first, last)}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # BSON -> plain JSON (relaxed extended JSON keeps int64 as numbers)
    explain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
