"""Independent reference implementation of the analytics, written straight from PROBLEM_STATEMENT.docx /
openapi.yaml with plain Python loops over raw documents. It deliberately shares NO code with app/main.py
(own rounding, own calendar logic) so that agreement between the two is meaningful."""
import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def hu(x, places):
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_range(month):
    y, mo = int(month[:4]), int(month[5:])
    return date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])


def in_month(rec, month):
    a, b = month_range(month)
    return a <= date.fromisoformat(rec["date"]) <= b


def is_weekday(day_str):
    return date.fromisoformat(day_str).weekday() < 5


def weight(rec):
    return Decimal("0.5") if rec.get("half_day") else Decimal(1)


def late(rec):
    return rec.get("late_minutes") or 0


def monthly(emps, logs, code, month):
    e = next(x for x in emps if x["emp_code"] == code)
    first, last = month_range(month)
    start = max(first, date.fromisoformat(e["joined_on"]))
    wd, d = 0, start
    while d <= last:
        wd += d.weekday() < 5
        d += timedelta(days=1)
    mine = [r for r in logs if r["emp_code"] == code and in_month(r, month)]
    present = sum((weight(r) for r in mine if r["status"] in PRESENT and is_weekday(r["date"])), Decimal(0))
    return {"emp_code": code, "month": month, "working_days": wd, "present_days": hu(present, 2),
            "leave_days": sum(r["status"] == "LEAVE" for r in mine),
            "late_count": sum(late(r) > 0 for r in mine), "total_late_minutes": sum(late(r) for r in mine),
            "total_overtime_minutes": sum(r.get("overtime_minutes") or 0 for r in mine),
            "attendance_pct": None if wd == 0 else hu(present / wd * 100, 2)}


def department_summary(emps, logs, month, department=None):
    _, last = month_range(month)
    out = {}
    for e in emps:
        if date.fromisoformat(e["joined_on"]) > last or (department is not None and e["department"] != department):
            continue
        out.setdefault(e["department"], []).append(e["emp_code"])
    items = []
    for dept in sorted(out):
        codes = set(out[dept])
        recs = [r for r in logs if r["emp_code"] in codes and in_month(r, month)]
        present = sum((weight(r) for r in recs if r["status"] in PRESENT and is_weekday(r["date"])), Decimal(0))
        hours = [Decimal(str(r["work_hours"])) for r in recs if r["status"] in PRESENT and r.get("work_hours") is not None]
        items.append({"department": dept, "headcount": len(codes), "present_days": hu(present, 2),
                      "avg_work_hours": hu(sum(hours) / len(hours), 2) if hours else None,
                      "late_count": sum(late(r) > 0 for r in recs), "total_late_minutes": sum(late(r) for r in recs),
                      "leave_count": sum(r["status"] == "LEAVE" for r in recs),
                      "on_duty_count": sum(r["status"] == "ON_DUTY" for r in recs)})
    return {"month": month, "items": items}


def leaderboard(emps, logs, month, limit=10, department=None):
    by_code = {e["emp_code"]: e for e in emps}
    totals = {}
    for r in logs:
        e = by_code.get(r["emp_code"])
        if e is None or not in_month(r, month) or late(r) <= 0:
            continue
        if department is not None and e["department"] != department:
            continue
        t = totals.setdefault(r["emp_code"], [0, 0])
        t[0] += late(r)
        t[1] += 1
    rows = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    ranked = []
    for code, (minutes, count) in rows:
        rank = 1 + sum(1 for _, (m2, _c) in rows if m2 > minutes)   # competition ranking
        e = by_code[code]
        ranked.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"],
                       "total_late_minutes": minutes, "late_count": count})
    return {"month": month, "items": [r for r in ranked if r["rank"] <= limit]}


def trend(emps, logs, department, d_from, d_to):
    dept_emps = [e for e in emps if e["department"] == department]
    codes = {e["emp_code"] for e in dept_emps}
    rows, day = [], d_from
    while day <= d_to:
        ds = day.isoformat()
        hc = sum(e["joined_on"] <= ds for e in dept_emps)
        recs = [r for r in logs if r["emp_code"] in codes and r["date"] == ds]
        present = sum((weight(r) for r in recs if r["status"] in PRESENT), Decimal(0))
        working = day.weekday() < 5
        rate = hu(present / hc, 4) if working and hc > 0 else None
        rows.append({"date": ds, "is_working_day": working, "headcount": hc, "present_count": hu(present, 2),
                     "late_count": sum(late(r) > 0 for r in recs), "attendance_rate": rate})
        day += timedelta(days=1)
    for i, row in enumerate(rows):
        window = [Decimal(str(r["attendance_rate"])) for r in rows[max(0, i - 6): i + 1] if r["attendance_rate"] is not None]
        row["moving_avg_7d"] = hu(sum(window) / len(window), 4) if window else None
    return {"department": department, "items": rows}
