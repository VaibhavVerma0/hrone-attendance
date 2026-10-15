# DECISIONS.md

1. **Indexes.**
   - Unique `employees.emp_code` and unique `attendance_logs(emp_code, date)`: these give the uniqueness guarantees, and they also serve the per-employee monthly query and the summary `$lookup`.
   - `logs(date desc, emp_code)`: the list sort and month-wide scans.
   - `logs(status, date desc, emp_code)`: status filters.
   - `logs(emp_code, punch_in desc)`: punch-out finds the latest open record.
   - `employees(department, joined_on)` and `employees(joined_on)`: headcount.
   - I rejected an index on `late_minutes`. The month's date range already narrows the leaderboard enough.

2. **Punch-in race.** Neither request checks first. Both call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed. The other gets `DuplicateKeyError`, which I turn into 409.

3. **Ties.** `$setWindowFields` with `$rank` gives competition ranks (1, 2, 2, 4). I filter on `rank <= limit`, not on row count, so everyone tied at the cutoff is returned. That means there can be more than `limit` rows, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` filtered by `joined_on <= month end`, not from logs. It `$lookup`s each person's logs and unwinds with `preserveNullAndEmptyArrays`, so people with no logs still add 1 to headcount.

5. **At 100x.** I would precompute daily per-department rollups when punches and corrections are written. The summary and trend endpoints would then read a few hundred rows instead of re-aggregating raw logs and running one `$lookup` per employee.
