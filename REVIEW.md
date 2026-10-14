# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `POST /attendance/punch-in` | Concurrent identical punch-ins could create duplicate attendance records. | Send two punch-in requests for the same employee and date concurrently. | Added a unique compound index on `emp_code` and `date`, and handled duplicate-key errors with HTTP 409. |
| 2 | `POST /attendance/punch-out` | Concurrent punch-out requests could attempt to update the same open attendance record. | Send two punch-out requests at nearly the same time. | Used a conditional update that only succeeds while `punch_out` is still `null`; a failed update returns HTTP 409. |
| 3 | `GET /analytics/leaderboard/late` | Employees tied at the cutoff could be omitted if the result were limited before ties were handled. | Create a tie at the requested rank limit. | Used competition ranking and included all employees whose rank is within the requested limit. |
| 4 | `GET /analytics/departments/summary` | Employees without attendance logs could be missed when calculating headcount from attendance data alone. | Add an eligible employee with no attendance records. | Calculated headcount from eligible employees before processing attendance logs. |
| 5 | `GET /analytics/departments/{department}/trend` | Moving averages and query-plan output needed correct date handling and aggregation support. | Test a multi-day trend and inspect its execution plan. | Used date-based moving-average processing and an aggregation explain command. |

**Reviewed and not treated as defects:** The unique attendance index, pagination validation, half-day calculations, and regularization conflict checks were tested and retained because they support the API's required behavior.