# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put them in a .env; real env vars win
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). `GET /health` returns 503 until MongoDB answers.

## Notes
- Instants are epoch milliseconds in the API and BSON dates in MongoDB. The client is `tz_aware` and all arithmetic is in UTC/IST-aware datetimes.
- Rounding: work hours and reported numbers are exact half-up (`Decimal`, or integer arithmetic inside the pipelines), never float `round`.
- Concurrency:
  - Punch-in and employee creation rely on unique indexes.
  - Punch-out is a conditional update on `punch_out: null`.
  - PATCH is an optimistic update guarded on the record's current state and its history length. The loser gets 409, so no history entry is lost.
- `moving_avg_7d` is the mean of the (rounded) `attendance_rate` values in the window, rounded half-up to 4 decimals.
- The trend range limit is 92 days inclusive (for example 1 Jul to 30 Sep).

