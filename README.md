# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0 or newer). All 12 endpoints of the contract are implemented. Nothing is missing.

## Run

    python -m venv .venv
    .venv\Scripts\activate          (macOS / Linux: source .venv/bin/activate)
    pip install -r requirements.txt
    uvicorn app.main:app --port 8000

Configuration comes from `MONGO_URI` and `MONGO_DB` (defaults: `mongodb://localhost:27017` and `attendance_db`).
A local `.env` is read for convenience, but real environment variables take priority. Indexes are created at startup.

## Tests

`tests/` holds scripts that compare the API against an independent pure-Python calculation (rules, analytics,
leaderboard ties, trend, explain plans) and run concurrency checks. They need the server and MongoDB running, insert
clearly named test data (`EMP9xxxx`, departments `ZZ_*`) and delete it afterwards:

    python tests/check_helpers.py
    python tests/check_analytics.py
    python tests/check_step6.py
    python tests/race_test.py
    python tests/race_test2.py

## Interpretation choices

- Trend: `moving_avg_7d` averages the reported (4-decimal) `attendance_rate` values; the 92-day limit counts both end dates.
- Rates and averages use exact half-up rounding (integer arithmetic inside the pipeline, `Decimal` in Python).
- Department `avg_work_hours` is the average over records, not an average of per-employee averages.
- A PATCH that changes nothing returns 422; concurrent PATCHes retry and never lose a history entry.
