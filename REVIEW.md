# REVIEW.md

Review of the starter `app/main.py`. "Reproduced" means I ran the starter's helper function on the input shown
(starter kept in git history). "By reading" means the defect follows directly from the code but I did not run the
starter endpoint against a database (no MongoDB was available in my environment; see README "Verification status").

| # | Where | What is wrong | How you'd notice it | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Applies the grace period to *floored* minutes: `int(seconds/60) > 10`. | **Reproduced:** shift 09:30, punch 09:40:01 returns 0 (contract: 10). | Compare whole seconds against 600 (`> GRACE_SECONDS`), then floor to minutes. |
| 2 | `compute_late_minutes` | Builds the shift start with `punch_in.replace(hour=...)` in whatever zone the datetime has. A UTC datetime (what PyMongo returns/what we store) gives the wrong shift start. | **Reproduced:** punch 09:50 IST passed as a UTC datetime returns 0 (want 20). | Shift start is built from the IST attendance date with an explicit IST offset (`ist_instant`). |
| 3 | `compute_late_minutes` / `punch_in` | No overnight handling: the shift start is taken from the punch's own calendar day, so a 22:00 shift punched at 05:30 next morning is dated and measured against the wrong day. | Shift 22:00-06:00, punch 05:30 IST on 07-07: starter dates it 07-07, contract says 07-06 (R1). | `attendance_date()` applies R1; late minutes are measured from that day's shift start. |
| 4 | `compute_work_hours` | `round()` is half-even on a binary float. | **Reproduced:** 9630 s (2.675 h) returns 2.67, R4 wants 2.68. Also no truncation to whole seconds. | `Decimal` division, `ROUND_HALF_UP`, inputs truncated to seconds in `ms_to_dt`. |
| 5 | `compute_overtime` | No 30-minute minimum, ignores overnight shift end, and mixes naive/aware datetimes. | **Reproduced:** punch-out 18:59:59 on shift end 18:30 returns 29 (want 0); overnight 06:40 returns 1480 (want 40); a naive punch-out raises `TypeError`. | `shift_end_instant()` (next day for overnight), floor to minutes, only `>= 30` counts. |
| 6 | `GET /health` | Always returns ok; never touches MongoDB. | Stop MongoDB: still 200. | `ping` with a bounded server-selection timeout; 503 on `PyMongoError`. |
| 7 | `EmployeeIn`, `PunchInIn` | No validation: any `emp_code`/email/date/shift string, `shift_start == shift_end`, any `status` (even `ABSENT`), `punched_at` as float/string/seconds. Pydantic lax mode even coerces `"123"` to int. | `status: "LEAVE"` accepted; `punched_at: 1783312500` accepted. | Patterns and bounds from openapi.yaml, `StrictInt` epoch-ms with 1e11..4.1e12 range, real-date check, shift-differ validator, `Literal` statuses. |
| 8 | `create_employee` | Check-then-insert race and no unique index: parallel creates can both succeed. | 20 parallel POSTs of one `emp_code` (integration test). | Unique index `uniq_emp_code`; `DuplicateKeyError` -> 409. |
| 9 | `create_employee` | `created_at = datetime.now()` is naive local time, and the response returns a datetime (ISO string) instead of epoch ms. | Response `created_at` is a string; value is host-local. | UTC `now_ms_precision()`; serialised as integer ms by `employee_out`. |
| 10 | `list_employees` | `skip = page * page_size` skips the whole first page; page 1 returns the second page. | Create 3 employees, `page=1&page_size=2` returns 1 item. | `(page - 1) * page_size`. |
| 11 | `list_employees` | `total` is `count_documents({})` ignoring the department filter; results are not sorted; `page`/`page_size` unbounded (0, -1, 10000); `if department:` ignores `?department=` (empty). | `?department=Sales` returns `total` of all employees. | Same filter for count and find, `sort emp_code`, `Query(ge=1, le=100)`, `is not None` test. |
| 12 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises -> HTTP 500. | POST with an unknown `emp_code`. | `get_employee_or_404`. |
| 13 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the **host** time zone and keeps microseconds; the date is therefore host-local, not IST. `if body.punched_at` treats a falsy value as "now". | **Reproduced** (`TZ=UTC`): a punch at 00:30 IST on 07-06 is dated 2026-07-05 by the starter's `fromtimestamp(...).date()`. | `ms_to_dt` (aware UTC, truncated), date via `attendance_date()` (IST), `is not None` test. |
| 14 | `punch_in` | Check-then-insert race and no unique index: simultaneous requests can create two records for the same day. | Parallel identical punch-ins. | Unique `(emp_code, date)` index; insert and map `DuplicateKeyError` -> 409. |
| 15 | `punch_in` | Response adds an `id` (the ObjectId as string): internal `_id` leaks, and `punch_in` is returned as a datetime string, not epoch ms. | Look at the response body. | `record_out()` builds the exact `AttendanceRecord` shape. |
| 16 | `list_attendance` | `list(find(q))` loads the whole collection into Python, sorts and slices there; sorted by `date` only (no `emp_code` tie-break); slice with `page=0` goes negative. | Time the endpoint on 100k records; ties come back in storage order. | `find().sort().skip().limit()` in MongoDB, indexes for the sort, `count_documents` with the same filter. |
| 17 | `list_attendance` | No validation of dates/status, `date_from > date_to` not rejected, returns `id` and datetimes, legacy documents without `half_day`/`history` are returned as-is. | `?date_from=2026-07-09&date_to=2026-07-01`; legacy record in the list. | `build_attendance_filter` (422s), `record_out` defaults. |
| 18 | module level | No indexes at all; client has no timeouts; PyMongo returns **naive** datetimes. | Explain shows `COLLSCAN`; hang when MongoDB is down. | `ensure_indexes()` in the lifespan (idempotent, never drops), timeouts on the client, `tz_aware=True`. |

**Examined and judged fine / kept**
- `load_dotenv()` with default `override=False`: real environment variables already win over `.env`. Kept.
- `MONGO_URI` / `MONGO_DB` have localhost development defaults. The grader injects its own values, so this is not hard-coded connection detail. Kept.
- Synchronous PyMongo inside plain `def` endpoints: FastAPI runs them in a thread pool, so requests are concurrent. Kept (no Motor).
- The `IST` constant itself (UTC+05:30) is correct and reused.
- Not-a-defect worth stating: the starter helpers are *not* reused as written (#1-#5); they were rewritten because their signatures hid the day/overnight context.

## Issues found in my own implementation during the verification pass
These are not starter defects; they were bugs or weak spots in my first version, found by the tests added afterwards.

| # | What was wrong | How it was found | Fix |
|---|---|---|---|
| A | `GET /employees` and `GET /attendance` used `estimated_document_count()` for unfiltered totals. R10 says `total` must be exact, and that call reads metadata that can be stale. | Review of R10 against the code. | `count_documents(filter)` always (exact; costs one count per request, to be measured on a real server). |
| B | Hand-raised 422s (date range, "changes nothing", 24 h rule, ...) returned `detail` as a string; the contract's `ValidationError` has `detail` as an array. | `tests/test_contract.py` (error-shape test). | `unprocessable()` raises `RequestValidationError` so the default FastAPI body is used everywhere. |
| C | A no-op `PATCH` on a record whose *stored* derived values disagree with a recomputation returned 200 and wrote a history entry, because "changes nothing" was judged on all seven tracked fields. DATA_MODEL.md says stored derived values are authoritative and the seeded data may hold such records. | `tests/test_contract.py` + new regression test in `tests/test_api.py`. | "Changes nothing" is judged on the client-editable fields (`status`, `punch_in`, `punch_out`). A real edit still records every derived field that differs. |
| D | My first concurrency tests (parallel requests) could not fail: removing the atomic guards from punch-out and regularize did not make them fail on the in-memory backend. They proved nothing about race safety. | Mutation check (guards deliberately removed). | Added `tests/test_race_windows.py`, which injects a competing write between the app's read and its write. With the guards removed 1-3 of its tests fail (4 mutants, all caught). The parallel tests are kept, but only the `real` backend can show MongoDB-level behaviour. |
| E | Four wrong *test* expectations of mine (e.g. 09:28-19:10 is 9.70 h, not 9.69; overtime unchanged is correctly absent from `changes`). | Failing tests whose application behaviour was right. | Corrected the tests, not the app. Hand-computed analytics values were additionally cross-checked with an independent oracle (`tests/oracle.py`). |
| F | One survivor in mutation testing: nothing checked that `avg_work_hours` ignores `work_hours` stored on ABSENT/LEAVE records. | Mutation check. | Added test and random-data coverage; mutant now caught. |

**Mutation testing** (deliberately breaking the pipelines and re-running the tests on the simulated backend): window size, no-log employees,
weekend presence, limit-before-rank, headcount ignoring `joined_on`, half-down rounding, late `>= 0`, average over all statuses, `joined_on <` vs `<=`.
All nine were caught after fix F. This shows the tests have teeth; it does not show the pipelines are accepted by a real MongoDB server.
