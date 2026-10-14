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
pytest                                                   # everything that can run without a MongoDB server
TEST_MONGO_URI=mongodb://localhost:27017 pytest -v       # same tests ALSO on a real MongoDB 6.0+ (random throw-away database, dropped afterwards)
RUN_PERF=1 TEST_MONGO_URI=... pytest tests/test_explain_perf.py -s   # seeds ~100k records, prints measured latency + explain facts
```
Most tests take a `backend_db` fixture with two parameters: `sim` (mongomock + `tests/minimongo.py`) and `real` (a MongoDB at
`TEST_MONGO_URI`). `real` is skipped when no server is reachable. Start one with `docker run -d -p 27017:27017 mongo:7`.

## Endpoints
All 12 are implemented: `GET /health`, `POST|GET /employees`, `POST /attendance/punch-in`, `POST /attendance/punch-out`,
`GET /attendance`, `PATCH /attendance/{emp_code}/{date}`, `GET /analytics/employees/{emp_code}/monthly`,
`GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend`,
`GET /admin/explain/{endpoint}`.

## Verification status (please read)
The environment I developed in had **no MongoDB** and could not download one, so nothing below was run against a real server.

| | Status |
|---|---|
| Business rules R1-R5, rounding, calendar helpers, request validation, OpenAPI paths/params, response and error bodies vs the JSON Schemas in `openapi.yaml`, CRUD, punch-in/out/regularize state machine, history, pagination | **Run and passing** (102 tests pass; no failures) |
| The 5 aggregation pipelines (monthly, department summary, leaderboard, trend; explain builds on them) | Run end to end through the real endpoints on a **simulator** I wrote (`tests/minimongo.py`), checked against an independent pure-Python oracle on hand-built and random data, and mutation-tested. A simulator is **not MongoDB**: it cannot show the server accepts the pipeline syntax. **Not yet run on a real server.** |
| Race conditions | Deterministic "competing write between read and write" tests pass (they fail if the guards are removed). **Not run against real MongoDB concurrency.** |
| Index creation: idempotent, no data loss, works over a pre-seeded DB | Passes on the simulator; the same tests run unchanged on a real server (not run) |
| `IXSCAN` / no `COLLSCAN` for the 5 explain targets, latency at 100k records | **Not measured.** `tests/test_explain_perf.py` is written for it; no numbers are claimed. |

Run `TEST_MONGO_URI=... pytest -v` and the perf command above before relying on the last three rows.

## Interpretations of the contract
- Unfiltered and filtered list totals use `count_documents` (exact), not `estimated_document_count`.
- Trend range: 92 calendar days inclusive is allowed; 93 is 422.
- Punch-out finds "the most recent record with punch_in <= punched_at"; if none, 404. "Already punched out" (409) is checked before the 422 time checks.
- A PATCH that changes none of `status`, `punch_in`, `punch_out` is 422, even if recomputing derived fields would differ from stored ones (stored values are authoritative).
- Analytics read stored derived values and do not filter records by `joined_on`; every record in the month counts.
- `moving_avg_7d` averages the *rounded* (4-decimal) daily rates, i.e. the reported `attendance_rate` values.
- Trend gap filling uses `$range` + `$unionWith` + `$group` rather than `$densify` (so it does not depend on `$densify` behaviour for an empty input).
- `punched_at: null` is treated as omitted ("now").
