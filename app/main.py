"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml. Stored data: DATA_MODEL.md. Rule numbers (R1 - R10) refer to openapi.yaml.
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority over .env

log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # datetimes come back as aware UTC, never naive
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"

# Epoch milliseconds: strict int (no floats, strings or bools), and not a seconds-looking value.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def unprocessable(msg: str, loc: tuple = ("body",)) -> HTTPException:
    """A 422 whose body has the same shape as FastAPI's own validation errors."""
    return HTTPException(422, detail=[{"type": "value_error", "loc": list(loc), "msg": msg}])


# --------------------------------------------------------------------------- #
# Time helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC, which is what BSON stores
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST date of the punch-in, except that an overnight-shift punch-in before shift_end
    belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d


def shift_start_at(att_date: date, emp: dict) -> datetime:
    return datetime.combine(att_date, parse_hhmm(emp["shift_start"]), tzinfo=IST)


def shift_end_at(att_date: date, emp: dict) -> datetime:
    end_day = att_date + timedelta(days=1) if is_overnight(emp) else att_date
    return datetime.combine(end_day, parse_hhmm(emp["shift_end"]), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, att_date: date, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = int((punch_in - shift_start_at(att_date, emp)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, att_date: date, emp: dict) -> int:
    """R3: floor minutes after shift end, counted only when >= 30."""
    minutes = int((punch_out - shift_end_at(att_date, emp)).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: hours rounded to 2 decimals half-up, computed exactly on whole seconds."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                   att_date: date, emp: dict) -> dict:
    """Every derived field for a record, from its final status and times (DATA_MODEL.md section 3)."""
    if status in NON_PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {"late_minutes": compute_late_minutes(punch_in, att_date, emp)}
    if punch_out is None:
        out.update(work_hours=None, overtime_minutes=0, half_day=False)
    else:
        hours = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=float(hours), overtime_minutes=compute_overtime(punch_out, att_date, emp),
                   half_day=hours < HALF_DAY_HOURS)
    return out


def round_half_up(value, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #
def parse_date(s: Optional[str], loc: tuple) -> Optional[date]:
    if s is None:
        return None
    if not re.fullmatch(DATE_RE, s):
        raise unprocessable("must be a date YYYY-MM-DD", loc)
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise unprocessable("must be a valid calendar date", loc)


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return first, last


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7)."""
    if end < start:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Serialization (MongoDB -> API)
# --------------------------------------------------------------------------- #
TIME_FIELDS = ("punch_in", "punch_out")


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in TIME_FIELDS:
            change = {"from": dt_to_ms(change.get("from")), "to": dt_to_ms(change.get("to"))}
        changes[field] = change
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Attendance record for the API; legacy records may omit half_day / history / derived values."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Indexes and app lifecycle
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    global _indexes_ready
    if _indexes_ready:
        return
    # Uniqueness guarantees (also serve emp_code lookups and per-employee date ranges).
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # Employees: department filter / headcount by joined_on, and headcount across all departments.
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # Attendance list sort (date desc, emp_code asc) and month-wide date-range scans.
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    # Punch-out: latest punch_in at or before a given instant for one employee.
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # do not block startup; /health retries once MongoDB answers
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
def mongo_error_handler(_, exc: PyMongoError):
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_date(cls, v: str) -> str:
        if not re.fullmatch(DATE_RE, v):
            raise ValueError("must be a date YYYY-MM-DD")
        date.fromisoformat(v)  # raises ValueError for e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Defaults are not validated, so an explicit null is rejected while an omitted field means "unchanged".
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=MONTH_RE)]
Limit = Annotated[int, Query(ge=1, le=50)]


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    try:
        ensure_indexes()  # no-op once done; retries if MongoDB was down at startup
    except PyMongoError as exc:
        log.error("index creation failed: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index makes concurrent duplicates fail here
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department is not None else {}
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_out(d) for d in cursor],
        "total": employees.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "history": [],
        **derived_fields(body.status, ts, None, att_date, emp),
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): of two racing inserts exactly one succeeds
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > MAX_SHIFT:
        raise unprocessable("punched_at must be after punch_in and within 24 hours", ("body", "punched_at"))
    update = {"punch_out": ts, **derived_fields(rec["status"], pin, ts, date.fromisoformat(rec["date"]), emp)}
    # Conditional update: only the request that still sees punch_out == null wins.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": update}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    d_from = parse_date(date_from, ("query", "date_from"))
    d_to = parse_date(date_to, ("query", "date_to"))
    if d_from and d_to and d_from > d_to:
        raise unprocessable("date_from must not be after date_to", ("query", "date_from"))
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = attendance_query(emp_code, date_from, date_to, status)
    cursor = logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [record_out(d) for d in cursor],
        "total": logs.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeIn, day: Annotated[str, Path(alias="date")]):
    att_date = parse_date(day, ("path", "date"))
    emp = get_employee(emp_code)
    cur = logs.find_one({"emp_code": emp_code, "date": day})
    if cur is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set
    status = body.status if "status" in sent else cur["status"]
    new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else cur.get("punch_in")
    new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else cur.get("punch_out")

    if status in NON_PRESENCE:
        if "punch_in" in sent or "punch_out" in sent:
            raise unprocessable(f"punch times cannot be set with status {status}")
        new_in = new_out = None
    else:
        if new_in is None:
            raise unprocessable(f"status {status} requires a punch_in", ("body", "punch_in"))
        if "punch_in" in sent and attendance_date(new_in, emp) != att_date:
            raise unprocessable("punch_in must fall on the record's attendance date", ("body", "punch_in"))
        if new_out is not None and ("punch_in" in sent or "punch_out" in sent):
            if new_out <= new_in or new_out - new_in > MAX_SHIFT:
                raise unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))

    old = {
        "status": cur["status"],
        "punch_in": cur.get("punch_in"),
        "punch_out": cur.get("punch_out"),
        "work_hours": cur.get("work_hours"),
        "late_minutes": int(cur.get("late_minutes") or 0),
        "overtime_minutes": int(cur.get("overtime_minutes") or 0),
        "half_day": bool(cur.get("half_day") or False),
    }
    new = {"status": status, "punch_in": new_in, "punch_out": new_out,
           **derived_fields(status, new_in, new_out, att_date, emp)}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        raise unprocessable("the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: the write only applies if the record is still exactly what we read
    # (same punch times, status and number of history entries); otherwise the loser gets 409.
    guard = {
        "_id": cur["_id"],
        "status": cur["status"],
        "punch_in": cur.get("punch_in"),
        "punch_out": cur.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(cur.get("history") or [])]},
    }
    updated = logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Aggregation building blocks
# --------------------------------------------------------------------------- #
IS_PRESENCE = {"$in": ["$status", list(PRESENCE)]}
PRESENCE_WEIGHT = {"$cond": [IS_PRESENCE, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE_MIN, 0]}


def count_if(cond) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENCE_WEIGHT, 0]}},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [IS_PRESENCE, {"$isNumber": "$work_hours"}]}
    return [
        # Start from employees so people with no logs are still counted (R9).
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENCE_WEIGHT, 0]}},
                    # work_hours has 2 decimals; summing hundredths as integers avoids float drift.
                    "wh_cents": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                    "wh_n": count_if(has_hours),
                    "late_count": count_if(IS_LATE),
                    "total_late_minutes": {"$sum": LATE_MIN},
                    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
            "wh_cents": {"$sum": {"$ifNull": ["$s.wh_cents", 0]}},
            "wh_n": {"$sum": {"$ifNull": ["$s.wh_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}]}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # Standard competition ranking (1, 2, 2, 4); limit is applied to the rank, not the row count.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def half_up_div(num, den, scale: int):
    """floor(num / den * scale + 0.5) as an expression on non-negative integers: exact half-up rounding."""
    return {"$floor": {"$divide": [{"$add": [{"$multiply": [num, 2 * scale]}, den]}, {"$multiply": [den, 2]}]}}


def trend_pipeline(department: str, codes: list, d_from: date, d_to: date) -> list:
    start = datetime.combine(d_from, time(), tzinfo=UTC)
    end_excl = datetime.combine(d_to + timedelta(days=1), time(), tzinfo=UTC)
    day = {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}}
    is_working = {"$lte": [{"$isoDayOfWeek": "$d"}, 5]}
    rate_ok = {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {"_id": "$date", "p2": {"$sum": {"$multiply": [PRESENCE_WEIGHT, 2]}}, "late": count_if(IS_LATE)}},
        # Seed the first day so $densify has something to fill from even when the range has no records.
        {"$unionWith": {"pipeline": [{"$documents": [{"_id": d_from.isoformat(), "p2": 0, "late": 0}]}]}},
        {"$group": {"_id": "$_id", "p2": {"$sum": "$p2"}, "late": {"$sum": "$late"}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]}}},
        {"$set": {"date": day, "p2": {"$ifNull": ["$p2", 0]}, "late": {"$ifNull": ["$late", 0]},
                  "is_working_day": is_working}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "as": "hc", "pipeline": [
            {"$match": {"department": department}},
            {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"},
        ]}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        # rate x 10000 as an exact half-up integer: present / headcount = p2 / (2 * headcount)
        {"$set": {"rate_e4": {"$cond": [rate_ok, half_up_div("$p2", {"$multiply": ["$headcount", 2]}, 10000), None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "w_sum": {"$sum": "$rate_e4", "window": {"documents": [-6, 0]}},
            "w_n": {"$sum": {"$cond": [{"$eq": ["$rate_e4", None]}, 0, 1]}, "window": {"documents": [-6, 0]}},
        }}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$divide": ["$p2", 2]},
            "late_count": "$late",
            "attendance_rate": {"$cond": [{"$eq": ["$rate_e4", None]}, None, {"$divide": ["$rate_e4", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$w_n", 0]}, None,
                                        {"$divide": [half_up_div("$w_sum", "$w_n", 1), 10000]}]},
        }},
    ]


def trend_args(department: str, from_: Optional[str], to: Optional[str]) -> tuple[list, date, date]:
    d_from = parse_date(from_, ("query", "from"))
    d_to = parse_date(to, ("query", "to"))
    if d_from is None or d_to is None:
        raise unprocessable("from and to are required", ("query",))
    if d_to < d_from:
        raise unprocessable("to must not be before from", ("query", "to"))
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"range must not exceed {MAX_TREND_DAYS} days", ("query", "to"))
    codes = [e["emp_code"] for e in employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    if not codes:
        raise HTTPException(404, "unknown department")
    return codes, d_from, d_to


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last)
    row = next(logs.aggregate(monthly_pipeline(emp_code, first, last)), None) or {}
    present = row.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": (
            float((Decimal(str(present)) * 100 / working_days).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if working_days else None
        ),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in employees.aggregate(department_summary_pipeline(first, last, department)):
        avg = None
        if r["wh_n"]:
            avg = float((Decimal(int(r["wh_cents"])) / 100 / r["wh_n"]).quantize(Decimal("0.01"), ROUND_HALF_UP))
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = list(logs.aggregate(leaderboard_pipeline(first, last, limit, department)))
    for it in items:
        it["total_late_minutes"] = int(it["total_late_minutes"])
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
):
    codes, d_from, d_to = trend_args(department, from_, to)
    return {"department": department, "items": list(logs.aggregate(trend_pipeline(department, codes, d_from, d_to)))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                        "department_trend"]


def require(value, name: str):
    if value is None:
        raise unprocessable(f"{name} is required for this endpoint", ("query", name))
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Annotated[ExplainTarget, Path()],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_RE)] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        coll = logs
        cmd = {"find": coll.name, "filter": attendance_query(emp_code, date_from, date_to, status),
               "projection": {"_id": 0}, "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size,
               "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            coll = logs
            first, last = month_bounds(require(month, "month"))
            pipeline = monthly_pipeline(require(emp_code, "emp_code"), first, last)
        elif endpoint == "department_summary":
            coll = employees
            first, last = month_bounds(require(month, "month"))
            pipeline = department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            coll = logs
            first, last = month_bounds(require(month, "month"))
            pipeline = leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            coll = logs
            codes, d_from, d_to = trend_args(require(department, "department"), from_, to)
            pipeline = trend_pipeline(department, codes, d_from, d_to)
        cmd = {"aggregate": coll.name, "pipeline": pipeline, "cursor": {}}
    result = db.command("explain", cmd, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": coll.name, "explain": json.loads(json_util.dumps(result))}
