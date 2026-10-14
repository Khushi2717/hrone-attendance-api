# DECISIONS.md

1. **Indexes.** employees: unique `emp_code`; `(department, emp_code)` for filtered sorted lists; `(department, joined_on)` and `(joined_on)` for headcount. attendance_logs: unique `(emp_code, date)` (race guard, monthly reads); `(date desc, emp_code)` for the default sort and month scans; `(status, date desc, emp_code)`; `(emp_code, punch_in desc)` to find the record to punch out. I rejected a covered `(date, late_minutes, emp_code)` index: more write cost for one query.
2. **Punch-in race.** Both requests derive the same date and insert. The unique `(emp_code, date)` index lets exactly one insert win (201); MongoDB raises a duplicate-key error for the other, which becomes 409. No read-then-insert.
3. **Ties.** Ranks come from `$setWindowFields`/`$rank` before the limit is applied, so every employee with rank <= limit is returned; a tie at the cutoff returns more rows than `limit`.
4. **Headcount.** The summary pipeline starts from `employees` (joined by month end), `$lookup`s the logs, and `$unwind` keeps empty results, so people with no logs still count.
5. **100x data.** Stop aggregating raw logs per request: keep per-employee monthly totals, updated on punch and correction.
