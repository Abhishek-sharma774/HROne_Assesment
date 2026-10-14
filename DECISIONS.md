# DECISIONS.md (about 180 words, in my own words)

1. **Indexes.** `employees.emp_code` is unique (duplicate 409 and fast lookup), `attendance(emp_code, date)` is unique (blocks double punch-ins and speeds up monthly queries), `(date, emp_code)` in both directions (fast list sorting plus leaderboard/trend matching), `(emp_code, punch_in)` (fast latest-open-record lookup for punch-out), and `(department, joined_on)` (fast headcount). Rejected: a standalone `status` index — status has only 5 values, so its selectivity is too low; the compound date indexes cover it.

2. **Punch-in race.** Both requests compute the R1 date and insert the same `(emp_code, date)` key. The unique index lets exactly one insert succeed (201); the other raises `DuplicateKeyError`, which becomes 409. There is no check-then-insert; the database is the judge.

3. **Ties.** Late totals are grouped in MongoDB, and ranks are computed in Python: sort by `total desc, emp_code asc`, share the rank on equal totals, and skip the next rank (1, 2, 2, 4). `limit` applies after ranking, so tied rows can return more than `limit` rows — that is what the contract requires.

4. **Headcount.** The summary starts from `employees` (`joined_on <= month_end`), then `$lookup` with a `preserveNullAndEmptyArrays` unwind. Employees with zero logs still produce one row, and headcount is counted with a separate `addToSet`, so nobody is missed.

5. **100x data.** Pre-aggregated daily/department rollup collections, archiving (or TTL/partitioning) of old attendance data, read replicas with cursor pagination, and covered indexes for hot queries. No full-collection loops in Python.
