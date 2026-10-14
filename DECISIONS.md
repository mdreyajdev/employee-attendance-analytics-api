# Design Decisions

## 1. Indexes

The unique employee-code index prevents duplicate employees. The
department and joined-date index supports employee filtering.
The unique employee-code/date index prevents duplicate attendance
records for the same employee and date. Additional attendance indexes
support date-based sorting, employee punch history, and status/date
queries. I would evaluate index usage with explain before adding more
indexes, because every extra index increases write and storage costs.

## 2. Punch-in race

If two identical punch-in requests arrive together, both may initially
find the employee. However, the unique employee-code/date index allows
only one attendance record for that date. The other insert raises
DuplicateKeyError and returns HTTP 409.

## 3. Leaderboard ties

The leaderboard ranks employees by total late minutes. Employees tied
at the cutoff share a rank, so the result can contain more entries than
the requested limit. Employee code provides deterministic ordering.

## 4. Department headcount

The department summary starts from employees and looks up attendance
logs. Preserving employees with no matching logs ensures they still
count towards headcount.

## 5. Scaling

For 100 times more data, I would inspect execution plans and query
latency, validate index usage, and benchmark aggregation pipelines
against a representative dataset before changing the design.