# Code Review

The starter implementation had correctness, validation, concurrency,
and analytics issues. The following changes were made.

| # | Where | What was wrong | How to notice | How it was fixed |
|---|---|---|---|---|
| 1 | Health endpoint | It reported success without checking MongoDB availability. | Stop MongoDB and call `/health`. | Added a database ping and return HTTP 503 when the ping fails. |
| 2 | Employee creation | Checking for duplicate employee codes before insertion is vulnerable to concurrent requests. | Send simultaneous requests with the same `emp_code`. | Added a unique index and handle `DuplicateKeyError` with HTTP 409. |
| 3 | Timestamp handling | Timestamps could be interpreted inconsistently or accept invalid values. | Send seconds, strings, floats, or invalid timestamp values. | Added strict integer epoch-millisecond validation and UTC conversion. |
| 4 | Attendance calculations | Late arrival, overtime, work hours, and half-day calculations needed consistent business rules. | Test boundary times around shift start and shift end. | Centralized derived calculations and applied the attendance rules. |
| 5 | Punch-in and punch-out | Duplicate attendance and concurrent updates could produce incorrect results. | Send duplicate punch-ins or simultaneous punch-outs. | Added a unique employee/date index and conditional updates. |
| 6 | Attendance regularization | Corrections needed validation and an audit trail. | Submit an invalid correction or a request that changes nothing. | Recalculate derived fields, validate corrections, record history, and detect concurrent changes. |
| 7 | Monthly analytics | Missing records and non-working days can distort attendance calculations. | Test an employee with no attendance records for a month. | Calculate working days and aggregate the relevant attendance records. |
| 8 | Department summary | Employees without attendance logs must still count towards headcount. | Include a department employee with no logs. | Start the aggregation from employees and preserve unmatched lookup results. |
| 9 | Late leaderboard | Employees tied at the limit must be handled consistently. | Create employees with equal total late minutes at the cutoff. | Use ranking by total late minutes and a stable employee-code sort. |
| 10 | Input validation | Invalid dates, months, status values, and timestamps must be rejected. | Submit malformed dates or unsupported status values. | Added date parsing, request validation, and appropriate HTTP errors. |

## Reviewed but not considered defects

- A weekday with no attendance record is not the same as an explicit ABSENT record.
- Derived attendance fields should be calculated by the API rather than trusted from client input.
- MongoDB `_id` is an internal identifier and should not be exposed in API responses.