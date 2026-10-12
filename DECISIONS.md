# DECISIONS.md 

1. **Indexes.**
We created unique indexes on `employees.emp_code` and `attendance_logs(emp_code, date)` to eliminate duplicate registrations and punch-in race conditions at the database level. For queries and sorting, we indexed `attendance_logs(date, status)` and `attendance_logs(emp_code, date)`. We considered individual indexes on every filter field, but rejected that to prevent write amplification.

2. **Punch-in race.**
Both concurrent requests validate inputs and attempt `insert_one`. MongoDB's unique compound index serializes them atomically. Exactly one insert succeeds with 201 Created. The duplicate attempt immediately throws `DuplicateKeyError`, which our exception handler intercepts to return 409 Conflict.

3. **Ties.**
Tied employees share a rank using standard competition ranking (`$rank`, producing 1, 2, 2, 4). When ties cross the `limit` boundary, all tied employees with `rank <= limit` are returned, sorted deterministically by `emp_code` ascending.

4. **Headcount.**
The department summary aggregation roots from `employees` rather than attendance logs. It filters employees by `joined_on <= month_end`, ensuring employees with zero logs are counted in headcount before left-joining attendance records via `$lookup`.

5. **One thing for 100x data.**
At 100x scale, we would maintain pre-aggregated daily rollups via MongoDB change streams or write-time updates, avoiding on-demand collection scans and full joins during analytics queries.
