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
On Windows (PowerShell):
```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # allows the activation script in this window only
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
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

## Verification status

Verified by running, on 12-13 October 2026: Windows 11, Python 3.14.4, a real local MongoDB 8.2.7 (no authentication), using throw-away databases that were dropped afterwards.

| Check | Result |
|---|---|
| Full suite, `pytest` | 178 passed, 0 failed, 1 skipped (the opt-in 100k test, run separately below) |
| Tests on the real server, `pytest -k real` | 75 passed: monthly, summary, leaderboard and trend results against hand-computed and independent-oracle values, contract-schema checks, index creation over pre-seeded data, parallel-request tests, race-window tests |
| Explain endpoint, small data | passed: `IXSCAN` present and `COLLSCAN` absent for all 5 targets |
| `RUN_PERF=1`, 105,395 attendance documents and 450 employees | passed. Best of 3 single requests, in-process, DB on the same machine: `GET /attendance` unfiltered (exact count) 70 ms, one month 14 ms, one employee 12 ms, status filter 16 ms; employee monthly 9 ms; department summary 626 ms; late leaderboard 107 ms; department trend (3 months) 286 ms |
| Explain stage details at 105,395 documents | the first stage of every pipeline uses an index scan; every `$lookup` reported `collectionScans = 0` and named its index (`uniq_emp_code_date`, `uniq_emp_code`, `dept_joined_on`) |
| Index creation over 105,395 existing documents | 1.91 s the first time, 0.00 s when run again (idempotent); the exact count afterwards matched the seeded total |
| `uvicorn app.main:app --port 8000` against the real server | started, `GET /health` returned 200 `{"status":"ok"}`, Swagger UI listed all 12 endpoints |

### Limits of this evidence
- Timings are single requests on a laptop, not load tests. The department summary is the slowest because it does one index lookup per employee, so it grows with employee count and records.
- The parallel-request tests passed on the real server, but a stress test cannot force a particular interleaving. The deterministic race-window tests inject a competing write between the application's read and write, which proves the guards (conditional updates, unique keys, retry), not MongoDB's own atomicity.
- The data was synthetic. The grader's dataset was not available.
- Only MongoDB 8.2.7 and Python 3.14.4 were run on the real server.
- Swagger UI shows request schemas but no response schemas (no `response_model` is declared). Response bodies are checked against the schemas in `openapi.yaml` by `tests/test_contract.py`.

## Interpretations of the contract
- Unfiltered and filtered list totals use `count_documents` (exact), not `estimated_document_count`.
- Trend range: 92 calendar days inclusive is allowed; 93 is 422.
- Punch-out finds "the most recent record with punch_in <= punched_at"; if none, 404. "Already punched out" (409) is checked before the 422 time checks.
- A PATCH that changes none of `status`, `punch_in`, `punch_out` is 422, even if recomputing derived fields would differ from stored ones (stored values are authoritative).
- Analytics read stored derived values and do not filter records by `joined_on`; every record in the month counts.
- `moving_avg_7d` averages the *rounded* (4-decimal) daily rates, i.e. the reported `attendance_rate` values.
- Trend gap filling uses `$range` + `$unionWith` + `$group` rather than `$densify` (so it does not depend on `$densify` behaviour for an empty input).
- `punched_at: null` is treated as omitted ("now").
