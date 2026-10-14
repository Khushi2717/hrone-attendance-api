"""Pure unit tests for the business rules R1-R5, rounding and time helpers. No database needed."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import main as m

IST = m.IST


def ist(y, mo, d, h, mi, s=0, ms=0):
    return datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=IST).astimezone(timezone.utc)


# ---- epoch helpers ------------------------------------------------------------------------------
def test_ms_to_dt_truncates_to_whole_seconds():
    assert m.ms_to_dt(1_783_312_500_999) == m.ms_to_dt(1_783_312_500_000)
    assert m.dt_to_ms(m.ms_to_dt(1_783_312_500_999)) == 1_783_312_500_000


def test_dt_to_ms_round_trip_and_naive_is_utc():
    dt = datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc)
    assert m.dt_to_ms(dt) == m.dt_to_ms(dt.replace(tzinfo=None))
    assert m.ms_to_dt(m.dt_to_ms(dt)) == dt


# ---- R1 attendance date ---------------------------------------------------------------------------
def test_day_shift_date_is_ist_date_not_utc_date():
    # 00:30 IST on 07-07 is 19:00 UTC on 07-06
    assert m.attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == date(2026, 7, 7)


def test_overnight_punch_before_shift_end_belongs_to_previous_day():
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == date(2026, 7, 6)
    assert m.attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == date(2026, 7, 7)
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == date(2026, 7, 6)


# ---- R2 late ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0),      # exactly 10:00 after start: not late
    (9, 40, 1, 10),     # 10:01 after start: late, minutes counted from shift start
    (10, 15, 59, 45),
    (9, 29, 0, 0),
    (14, 30, 0, 300),
])
def test_late_minutes_contract_examples(h, mi, s, expected):
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), date(2026, 7, 6), "09:30") == expected


def test_late_minutes_overnight_shift_uses_shift_start_day():
    day = date(2026, 7, 6)
    assert m.compute_late_minutes(ist(2026, 7, 6, 22, 10, 0), day, "22:00") == 0
    assert m.compute_late_minutes(ist(2026, 7, 6, 22, 10, 1), day, "22:00") == 10
    assert m.compute_late_minutes(ist(2026, 7, 6, 21, 55), day, "22:00") == 0


# ---- R3 overtime ----------------------------------------------------------------------------------------
def test_overtime_threshold_and_floor():
    end = ist(2026, 7, 6, 18, 30)
    assert m.compute_overtime_minutes(ist(2026, 7, 6, 18, 59, 59), end) == 0
    assert m.compute_overtime_minutes(ist(2026, 7, 6, 19, 0, 0), end) == 30
    assert m.compute_overtime_minutes(ist(2026, 7, 6, 19, 10, 59), end) == 40
    assert m.compute_overtime_minutes(ist(2026, 7, 6, 17, 0), end) == 0


def test_overnight_shift_end_is_next_day():
    end = m.shift_end_instant(date(2026, 7, 6), "22:00", "06:00")
    assert end == ist(2026, 7, 7, 6, 0)
    assert m.compute_overtime_minutes(ist(2026, 7, 7, 6, 40), end) == 40  # sample document 7


# ---- R4 / R5 work hours and half day ---------------------------------------------------------------------------
def test_work_hours_half_up_not_bankers():
    t0 = ist(2026, 7, 6, 9, 0)
    assert m.compute_work_hours(t0, t0 + timedelta(seconds=16182)) == 4.50   # 4.495 -> 4.50 (half-up)
    assert m.compute_work_hours(t0, t0 + timedelta(seconds=16181)) == 4.49   # 4.49472..
    assert m.compute_work_hours(t0, t0 + timedelta(hours=3, minutes=30)) == 3.5
    assert m.compute_work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12  # sample 1


def test_half_day_uses_rounded_hours():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    day = date(2026, 7, 6)
    t0 = ist(2026, 7, 6, 9, 0)
    assert m.derive_fields("PRESENT", t0, t0 + timedelta(seconds=16182), day, emp)["half_day"] is False  # 4.495 -> 4.50
    assert m.derive_fields("PRESENT", t0, t0 + timedelta(seconds=16181), day, emp)["half_day"] is True   # 4.49
    assert m.derive_fields("WFH", t0, t0 + timedelta(hours=3, minutes=30), day, emp)["half_day"] is True


def test_derive_fields_defaults():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    day = date(2026, 7, 6)
    assert m.derive_fields("ABSENT", None, None, day, emp) == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    open_rec = m.derive_fields("PRESENT", ist(2026, 7, 6, 10, 5), None, day, emp)
    assert open_rec == {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}


def test_sample_document_2_late_with_overtime():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    d = m.derive_fields("PRESENT", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10), date(2026, 7, 6), emp)
    assert d == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}


# ---- rounding / calendar ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value,places,expected", [
    (Decimal("0.125"), 2, "0.13"), (Decimal("2.675"), 2, "2.68"), (0.5, 0, "1"),
    (Decimal("0.41665"), 4, "0.4167"), (Decimal("0.00005"), 4, "0.0001"),
])
def test_round_half_up(value, places, expected):
    assert str(m.round_half_up(value, places)) == expected


def test_count_weekdays():
    assert m.count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.count_weekdays(date(2026, 7, 4), date(2026, 7, 5)) == 0      # Sat, Sun
    assert m.count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15   # mid-month joiner


def test_parse_month_and_date_validation():
    assert m.parse_month("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    for bad in ("2026-13", "2026-1", "0000-01"):
        with pytest.raises(m.HTTPException):
            m.parse_month(bad)
    for bad in ("2026-02-30", "20260201", "2026-2-1"):
        with pytest.raises(m.HTTPException):
            m.parse_date(bad)


def test_trend_range_limits():
    assert m.parse_trend_range("2026-07-01", "2026-09-30")[1] == date(2026, 9, 30)  # 92 days inclusive
    with pytest.raises(m.HTTPException):
        m.parse_trend_range("2026-07-01", "2026-10-01")  # 93 days
    with pytest.raises(m.HTTPException):
        m.parse_trend_range("2026-07-02", "2026-07-01")


# ---- pipeline shape (no database): the requirements that must hold in the built pipelines ------------------
def test_leaderboard_pipeline_uses_rank_window_and_limits_after_ranking():
    p = m.late_leaderboard_pipeline("2026-07-01", "2026-07-31", 3, None)
    names = [next(iter(s)) for s in p]
    assert names.index("$setWindowFields") < names.index("$match", names.index("$setWindowFields"))
    assert p[names.index("$setWindowFields")]["$setWindowFields"]["output"]["rank"] == {"$rank": {}}
    assert "$limit" not in names


def test_trend_pipeline_has_no_python_generated_dates_and_uses_window():
    p = m.department_trend_pipeline("Sales", date(2026, 7, 1), date(2026, 7, 31))
    text = str(p)
    assert "$range" in text and "$setWindowFields" in text and "[-6, 0]" in text
    assert "2026-07-15" not in text  # only the range bounds are literals


def test_department_summary_pipeline_starts_from_employees_and_keeps_empty_lookups():
    p = m.department_summary_pipeline("2026-07-01", "2026-07-31", None)
    assert p[0] == {"$match": {"joined_on": {"$lte": "2026-07-31"}}}
    unwind = next(s for s in p if "$unwind" in s)["$unwind"]
    assert unwind["preserveNullAndEmptyArrays"] is True
