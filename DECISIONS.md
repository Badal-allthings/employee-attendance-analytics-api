# DECISIONS.md

1. **Indexes:** I created a unique index on `emp_code` to prevent duplicate employees. A unique compound index on `emp_code` and `date` guarantees one attendance record per employee per day and prevents duplicate punch-ins. The `date` and `emp_code` index supports attendance filtering and sorting. The `department` and `joined_on` index supports department-based employee selection. I did not add a separate status-only index because status filters can match many records and are often combined with other filters.

2. **Punch-in race:** If two identical punch-in requests arrive together, both may initially find that no attendance record exists. The unique compound index allows only one insert to succeed. MongoDB rejects the other insert with a duplicate-key error, and the API returns HTTP 409 Conflict.

3. **Ties:** The leaderboard uses competition ranking, such as 1, 2, 2, 4. It returns every employee whose rank is within the requested limit, including employees tied at the cutoff.

4. **Headcount:** The department summary first selects employees who joined by the end of the requested month. It calculates headcount from those employees before processing attendance logs, so employees with no logs are still counted.

5. **Scaling:** For 100 times more data, I would consider pre-aggregating daily attendance statistics and updating them incrementally to reduce repeated analytics calculations.