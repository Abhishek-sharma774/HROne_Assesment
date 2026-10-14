# REVIEW.md — defects found in the starter `app/main.py`

I read the starter file like a teammate's pull request and tested each part
with real inputs. Every defect below is fixed in this submission.
Short version: time math ignored seconds/timezone, rounding was wrong,
pagination skipped rows, races were possible, responses leaked internals,
validation had holes, and there were no indexes.

| # | Where | What is wrong (in simple words) | How you would notice it | How I fixed it |
|---|---|---|---|---|
| 1 | Late calculation | Grace check was `minutes > 10`. A punch at `09:40:01` becomes `int(10.01) = 10`, and `10 > 10` is false, so it returned 0 late instead of 10. Seconds and IST were ignored. | Shift 09:30, punch `09:40:01` IST: starter says 0 late, correct is 10. | Difference is measured in seconds in IST: `diff <= 600 → 0`, otherwise `floor(diff / 60)`. |
| 2 | Work-hours rounding | Used `round()`, which is banker's rounding. The rule needs half-up rounding. | Some durations round one cent the wrong way (e.g. `2.675 → 2.67` instead of `2.68`). | Used `Decimal(...).quantize(0.01, ROUND_HALF_UP)`. |
| 3 | Overtime | The 30-minute minimum was missing, overnight shifts never moved the shift end to the next day, and datetimes were naive (no IST/UTC). | Shift 18:30, out 18:35: starter says 5 min OT, correct is 0. Overnight 22:00–06:00 was fully wrong. | Built the IST end time (overnight +1 day); overtime counts only when `>= 30`. |
| 4 | Employee input model | No `emp_code` pattern, no email pattern, no `joined_on` date check. | `emp_code: "ABC"` or `email: "not-an-email"` got 201 instead of 422. | Added Pydantic patterns, a `joined_on` date check, and a `shift_start != shift_end` check (422). |
| 5 | Create employee | Find-then-insert with no unique index, so two parallel requests could both return 201. `created_at` was naive and the response sent a datetime instead of millis. | Parallel duplicate POSTs created 2 records. Response showed a datetime object. | Unique index on `emp_code` + `DuplicateKeyError → 409`. UTC-aware `created_at`, millis in the response. |
| 6 | List employees | `skip = page * page_size` (page 1 hides the first 20 rows). `total` ignored the filter. No `emp_code` sort. | Page 1 misses the first 20 rows. `?department=Sales` still reported the full total. | `skip = (page - 1) * size`, `count(filtered)`, `.sort(emp_code)`. |
| 7 | Punch-in | Unknown employee crashed with 500 (KeyError) instead of 404. `status` was not checked (ABSENT accepted). Date used the local clock instead of IST + overnight logic. Check-then-insert race. Response had an `id` field and raw datetimes. | Wrong emp_code → 500. `status: ABSENT` → 201. Two simultaneous punches → both 201. | 404 check, only PRESENT/WFH/ON_DUTY allowed, R1 date helper, unique `(emp_code, date)` index with 409 handling, millis response, no `id`. |
| 8 | List attendance | Loaded the whole collection into memory and sorted in Python (dies at 100k). Sorted by date only, no `emp_code` tie-break. No 422 for `date_from > date_to`. Sent raw datetimes. | Very slow at 100k records. Same-date rows in random order. | Sort + skip + limit inside MongoDB, `count_documents`, 422 validation, millis responses. |
| 9 | Health check | Never pinged MongoDB, always returned 200, even with the database down. | Stop MongoDB and call `/health`: starter says 200, correct is 503. | `client.admin.command("ping")`; on failure return 503. |
| 10 | Time format | Accepted seconds, floats and strings for `punched_at` (no millis range check). Never truncated instants to whole seconds. | `punched_at: 123` or `12.5` accepted instead of 422. `09:40:00.900` stored wrongly. | Pydantic `ge/le` range, `ms // 1000` truncation; stored and returned value is the truncated one. |
| 11 | Indexes | No indexes at all. No uniqueness guarantee, slow at 100k, explain plans show COLLSCAN. | `/admin/explain` shows COLLSCAN; double punch-in race possible. | 7 indexes at startup (unique emp_code, unique emp_code+date, date sorts, punch_in, department). |
| 12 | Response shape | Sent an `id` field that is not in the contract (a record is addressed by `(emp_code, date)`). Old documents missing `half_day`/`history` were not handled. | Extra `id` in responses; legacy doc (EMP0002 sample 9) could crash formatting. | `format_attendance_for_api`: no id, millis times, safe defaults for missing fields. |
| 13 | Punch time type | Plain `int` with range only. Pydantic quietly converts whole floats (`1783312500000.0`) and numeric strings (`"123"`) to int, but the contract rejects all floats and strings with 422. | `punched_at: 1783312500000.0` got 201 instead of 422. | Used `StrictInt` for every epoch-millis field, keeping the `ge/le` range. |
| 14 | Blank text | `name`, `department`, `reason` and `regularized_by` accepted whitespace-only values like `" "`. | `name: " "` created an employee with a blank name (201). | A small validator rejects whitespace-only values with 422. |

Checked and found **not** to be defects:
- `load_dotenv()` without override already lets real environment variables win, so that requirement was already met.
- The `PunchInIn.status = "PRESENT"` default was correct; only the validation was missing.
