"""Shared test data builders."""
from datetime import datetime, timezone

from app import main as m


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def emp(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"N {code}", "email": f"{code}@x.io", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": utc(2026, 1, 1)}


def log(code, day, status="PRESENT", late=0, ot=0, wh=None, half=False):
    present = status in m.PRESENT_STATUSES
    return {"emp_code": code, "date": day, "status": status, "late_minutes": late, "overtime_minutes": ot,
            "work_hours": wh, "half_day": half, "history": [],
            "punch_in": utc(*map(int, day.split("-")), 4, 0) if present else None, "punch_out": None}


def seed_july(db):
    """July 2026: Wed 07-01 ... Fri 07-31 -> 23 weekdays. Weekends: 4,5,11,12,18,19,25,26.
    Engineering has a mid-month joiner (EMP1002), an employee without logs (EMP1003) and a future joiner (EMP1004);
    Ops joins after July; EMP9999 has logs but no employee document."""
    db.employees.insert_many([
        emp("EMP1001", "Engineering", "2026-06-01"), emp("EMP1002", "Engineering", "2026-07-13"),
        emp("EMP1003", "Engineering", "2026-07-01"), emp("EMP1004", "Engineering", "2026-08-05"),
        emp("EMP2001", "Sales", "2026-01-01"), emp("EMP2002", "Sales", "2026-01-01"),
        emp("EMP3001", "Ops", "2026-09-01"),
    ])
    db.attendance_logs.insert_many([
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
