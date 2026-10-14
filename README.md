# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne contract (`openapi.yaml`, `DATA_MODEL.md`, rules R1-R10).
All application code is in `app/main.py`.

## Run
Prerequisites: Python 3.11+, MongoDB 6.0+ (local, Docker or Atlas).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # or export MONGO_URI / MONGO_DB (environment variables win over .env)
uvicorn app.main:app --port 8000
```
Swagger UI: http://localhost:8000/docs. Indexes are created at startup (idempotent; nothing is dropped or deleted).

Load the 9 sample documents into a **disposable** database (this script empties both collections first):
```bash
MONGO_DB=attendance_sample python sample_seed.py
```

## Tests
```bash
pip install -r requirements-dev.txt
pytest                                        # rule unit tests + CRUD tests (mongomock); integration tests skip without MongoDB
TEST_MONGO_URI=mongodb://localhost:27017 pytest tests/test_integration.py -v      # needs a real MongoDB
RUN_PERF=1 TEST_MONGO_URI=... pytest tests/test_integration.py -k 100k -s          # seeds ~100k docs, prints latencies
```
Integration tests create a randomly named database and drop it afterwards.

## Endpoints
All 12 are implemented: `GET /health`, `POST|GET /employees`, `POST /attendance/punch-in`, `POST /attendance/punch-out`,
`GET /attendance`, `PATCH /attendance/{emp_code}/{date}`, `GET /analytics/employees/{emp_code}/monthly`,
`GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend`,
`GET /admin/explain/{endpoint}`.

## Verification status (be aware)
The development sandbox had **no MongoDB** (and could not download one), so:
- **Verified by running:** business rules R1-R5 and rounding, request validation (422s), CRUD, punch-in/out and
  regularization state machine, history handling, pagination/sorting (mongomock), pipeline *shapes*.
- **Written but NOT yet executed against a real MongoDB:** the five aggregation pipelines (monthly, department summary,
  leaderboard, trend, and the explain endpoint), real concurrency behaviour, and the `IXSCAN`/no-`COLLSCAN` plans.
  Tests for them exist in `tests/test_integration.py` with expected values worked out by hand; run them before submitting.

## Interpretations
- Trend range limit: 92 calendar days inclusive is allowed; 93 is 422.
- Punch-out checks "already punched out" (409) before the 422 time checks.
- Analytics read stored derived values and do not filter records by `joined_on`; they count every record in the month.
- `moving_avg_7d` averages the *rounded* (4-decimal) daily rates, as the contract defines it over `attendance_rate` values.
- Trend gap filling uses `$range` + `$unionWith` + `$group` rather than `$densify` (no dependence on `$densify` input behaviour).
