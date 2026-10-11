"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)
"""
import os
import re
import calendar
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

load_dotenv()  # override=False -> real environment variables take priority

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                    # always get aware UTC datetimes back
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent)
# --------------------------------------------------------------------------- #
# (collection, keys, options). Each one serves a specific query or guarantee.
INDEXES = [
    # emp_code is the business identity: uniqueness is enforced by the database, not by a pre-check
    ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "uq_emp_code"}),
    # GET /employees?department=X, sorted by emp_code
    ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "ix_dept_emp"}),
    # department summary starts from employees with joined_on <= end of month
    ("employees", [("joined_on", ASCENDING)], {"name": "ix_joined"}),
    ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "ix_dept_joined"}),
    # one record per employee per day (punch-in race guard); per-employee and per-month queries
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uq_emp_date"}),
    # default list order (date desc, emp_code asc) and month-wide scans (leaderboard)
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_date_emp"}),
    # status filter with the same sort
    ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
     {"name": "ix_status_date_emp"}),
    # punch-out: most recent record of this employee with punch_in <= t
    ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "ix_emp_punchin"}),
]


def ensure_indexes() -> None:
    """Idempotent. Every index is created on its own, so one failure (for example a unique
    index over dirty data) cannot stop the others. If the database is unreachable we stop
    after the first timeout and keep the app up so /health can answer 503."""
    for collection, keys, options in INDEXES:
        try:
            db[collection].create_index(keys, **options)
        except ServerSelectionTimeoutError as exc:
            print(f"[startup] database unreachable, skipping index creation: {exc}")
            return
        except PyMongoError as exc:
            print(f"[startup] index {options['name']} not created: {exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers (instants <-> epoch ms, IST calendar logic)
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    """Naive datetimes are treated as UTC (that is how Mongo stores them)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    n = datetime.now(timezone.utc)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)  # Mongo keeps ms


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def round_half_up(value, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules R1 - R5 (pure functions, no database)
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in; overnight shifts started the day before
    if the punch-in is earlier than shift_end."""
    local = as_utc(punch_in).astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if local.time() < dtime(eh, em):
            d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Shift start/end instants (aware, IST) for the record's attendance date."""
    d = date.fromisoformat(date_str)
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end_day = d + timedelta(days=1) if is_overnight(shift_start, shift_end) else d
    end = datetime(end_day.year, end_day.month, end_day.day, eh, em, tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor minutes
    since shift_start (not since the end of grace)."""
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    delta = int((as_utc(punch_in) - start).total_seconds())
    return delta // 60 if delta > 600 else 0


def compute_overtime_minutes(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift_end, counted only if >= 30."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    mins = int((as_utc(punch_out) - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (exact decimal arithmetic)."""
    secs = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.5


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.fullmatch(v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # rejects 2026-02-30 etc.
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


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


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index decides the race, not a pre-check
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance: shared helpers
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def to_api(value):
    """Convert datetimes to epoch ms, recursively (needed inside history.changes)."""
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: to_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_api(v) for v in value]
    return value


def record_out(doc: dict) -> dict:
    """Stored document -> API shape. No _id; missing legacy fields get defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": to_api(doc.get("history") or []),
    }


def check_date(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    if not DATE_RE.fullmatch(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a real calendar date")
    return value


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """Used by GET /attendance and (later) by the explain endpoint, so both match."""
    date_from = check_date(date_from, "date_from")
    date_to = check_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


# --------------------------------------------------------------------------- #
# Attendance: punch-in and list
# --------------------------------------------------------------------------- #
class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=20)
    # StrictInt: floats and strings are rejected (422); range rejects "seconds" values
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: PresenceStatus = "PRESENT"


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is None:
        ts = now_utc().replace(microsecond=0)
    else:
        ts = ms_to_dt(body.punched_at)  # truncated to whole seconds (R1)

    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index decides the race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Derived fields and shared checks (R2 - R5), used by punch-out and PATCH
# --------------------------------------------------------------------------- #
MAX_SHIFT = timedelta(hours=24)
NO_PUNCH_STATUSES = ("ABSENT", "LEAVE")


def check_punch_out_window(punch_in: datetime, punch_out: datetime) -> None:
    if as_utc(punch_out) <= as_utc(punch_in):
        raise HTTPException(422, "punch_out must be after punch_in")
    if as_utc(punch_out) - as_utc(punch_in) > MAX_SHIFT:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


def derived_fields(emp: dict, date_str: str, status: str, punch_in, punch_out) -> dict:
    """work_hours, late_minutes, overtime_minutes, half_day from the FINAL values."""
    if status in NO_PUNCH_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    ss, se = emp["shift_start"], emp["shift_end"]
    late = compute_late_minutes(punch_in, date_str, ss, se)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, date_str, ss, se),
        "half_day": compute_half_day(hours),
    }


def values_equal(a, b) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return as_utc(a) == as_utc(b)
    return a == b


# --------------------------------------------------------------------------- #
# Punch-out
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=20)
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is None:
        ts = now_utc().replace(microsecond=0)
    else:
        ts = ms_to_dt(body.punched_at)

    # most recent record whose punch_in is at or before the punch-out (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_out_window(rec["punch_in"], ts)

    fields = derived_fields(emp, rec["date"], rec["status"], rec["punch_in"], ts)
    fields["punch_out"] = ts
    # atomic: only the request that still sees punch_out == null can win
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Regularization (PATCH)
# --------------------------------------------------------------------------- #
class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # derived fields sent by a client are simply ignored (pydantic drops unknown keys)


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn):
    """Pure logic, no database: returns (fields to $set, history 'changes')."""
    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status

    if new_status in NO_PUNCH_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE clear the punch times; do not send them")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            check_punch_out_window(new_in, new_out)

    if (new_status == old_status and values_equal(new_in, old_in)
            and values_equal(new_out, old_out)):
        raise HTTPException(422, "the request changes nothing")

    new_fields = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new_fields.update(derived_fields(emp, rec["date"], new_status, new_in, new_out))

    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {
        k: {"from": old_values[k], "to": v}
        for k, v in new_fields.items()
        if not values_equal(old_values[k], v)
    }
    return new_fields, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    check_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    for _ in range(5):  # optimistic concurrency: re-read and retry if someone else won
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new_fields, changes = plan_regularization(emp, rec, body)
        entry = {
            "at": now_utc(),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }
        updated = db.attendance_logs.find_one_and_update(
            {
                "_id": rec["_id"],
                "status": rec["status"],
                "punch_in": rec.get("punch_in"),
                "punch_out": rec.get("punch_out"),
                # history only ever grows, so its length is the record's version
                "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
            },
            {"$set": new_fields, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# Analytics: shared helpers
# --------------------------------------------------------------------------- #
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise HTTPException(422, "month year out of range")
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def weekdays_between(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7). A tiny calendar loop, not a data loop."""
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def weekend_dates(start: date, end: date) -> list[str]:
    out, d = [], start
    while d <= end:
        if d.weekday() >= 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def quantize_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def log_sums(weekends: list[str]) -> dict:
    """$group accumulators over attendance logs. They read the STORED derived values
    (late_minutes, overtime_minutes, half_day, work_hours) and never recompute them."""
    is_present = {"$in": ["$status", PRESENT_STATUSES]}
    on_weekday = {"$not": [{"$in": ["$date", weekends]}]}
    day_value = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
    late = {"$ifNull": ["$late_minutes", 0]}
    has_hours = {"$and": [is_present, {"$isNumber": "$work_hours"}]}
    return {
        "present_days": {"$sum": {"$cond": [{"$and": [is_present, on_weekday]}, day_value, 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": late},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # hours are summed as integer hundredths so the average can be rounded exactly
        "hours_cents": {"$sum": {"$cond": [has_hours, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
        "hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
    }


# --------------------------------------------------------------------------- #
# Analytics: employee monthly summary
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None, **log_sums(weekend_dates(first, last))}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")

    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    s = rows[0] if rows else {}

    start = max(first, date.fromisoformat(emp["joined_on"]))  # R7: count from joined_on
    working_days = weekdays_between(start, last) if start <= last else 0
    present = Decimal(str(s.get("present_days", 0)))
    pct = quantize_half_up(present * 100 / working_days, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": quantize_half_up(present, 2),
        "leave_days": int(s.get("leave_count", 0)),
        "late_count": int(s.get("late_count", 0)),
        "total_late_minutes": int(s.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(s.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Analytics: department summary (employees LEFT JOIN logs, so no-log people count)
# --------------------------------------------------------------------------- #
SUMMARY_FIELDS = ["present_days", "leave_count", "on_duty_count", "late_count",
                  "total_late_minutes", "hours_cents", "hours_count"]


def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9: headcount by joined_on
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {"_id": None, **log_sums(weekend_dates(first, last))}},
            ],
            "as": "m",
        }},
        # an employee with no logs gets an empty object, but still counts in headcount
        {"$set": {"m": {"$ifNull": [{"$first": "$m"}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            **{f: {"$sum": {"$ifNull": [f"$m.{f}", 0]}} for f in SUMMARY_FIELDS},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        count = int(r["hours_count"])
        avg = None
        if count:  # average over RECORDS: total hours / number of records
            avg = quantize_half_up(Decimal(int(r["hours_cents"])) / Decimal(count) / Decimal(100), 2)
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": quantize_half_up(Decimal(str(r["present_days"])), 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# 422 bodies: every validation error uses FastAPI's default {"detail": [ ... ]} shape
# --------------------------------------------------------------------------- #
@app.exception_handler(StarletteHTTPException)
async def _http_exception(request, exc):
    if exc.status_code == 422 and isinstance(exc.detail, str):
        body = {"detail": [{"type": "value_error", "loc": ["request"], "msg": exc.detail, "input": None}]}
        return JSONResponse(status_code=422, content=body)
    return await http_exception_handler(request, exc)


# --------------------------------------------------------------------------- #
# Analytics: late leaderboard (standard competition ranking via $setWindowFields)
# --------------------------------------------------------------------------- #
def late_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": late},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # inner join: logs whose employee does not exist disappear here
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},
    ]
    if department is not None:  # rank inside the department only, so filter BEFORE ranking
        pipeline.append({"$match": {"e.department": department}})
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name",
                      "department": "$e.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = []
    for r in db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)):
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: department trend ($densify gap filling + $setWindowFields moving average)
# --------------------------------------------------------------------------- #
MAX_TREND_DAYS = 92


def trend_range(date_from: str, date_to: str) -> tuple[date, date]:
    d_from = date.fromisoformat(check_date(date_from, "from"))
    d_to = date.fromisoformat(check_date(date_to, "to"))
    if d_to < d_from:
        raise HTTPException(422, "to must not be before from")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_TREND_DAYS} days")
    return d_from, d_to


def department_trend_pipeline(codes: list, joined_dates: list, d_from: date, d_to: date) -> list:
    """Rates are computed as integers in ten-thousandths so that 'half-up, 4 decimals' is
    exact (Mongo's $round is banker's rounding). floor((x*2 + n) / (2n)) == round-half-up(x/n)."""
    lower = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    upper = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)  # exclusive
    is_present = {"$in": ["$status", PRESENT_STATUSES]}
    half_aware = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
    late = {"$ifNull": ["$late_minutes", 0]}
    return [
        {"$match": {"emp_code": {"$in": codes},
                    "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [is_present, half_aware, 0]}},  # weekends count here
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        # one row per calendar day, including days with no records at all
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},  # 1 = Sunday
        }},
        # R9: employees with joined_on <= that day
        {"$set": {"headcount": {"$size": {"$filter": {
            "input": {"$literal": joined_dates}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}}},
        {"$set": {"rate_e4": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$present_count", 20000]}, "$headcount"]},
                {"$multiply": [2, {"$max": ["$headcount", 1]}]}]}},
            None]}}},
        # current row + up to 6 preceding rows INSIDE the range; null rates are ignored
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "win_sum": {"$sum": "$rate_e4", "window": {"documents": [-6, 0]}},
            "win_n": {"$sum": {"$cond": [{"$isNumber": "$rate_e4"}, 1, 0]}, "window": {"documents": [-6, 0]}},
        }}},
        {"$set": {"avg_e4": {"$cond": [
            {"$gt": ["$win_n", 0]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$win_sum", 2]}, "$win_n"]},
                {"$multiply": [2, {"$max": ["$win_n", 1]}]}]}},
            None]}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$isNumber": "$rate_e4"}, {"$divide": ["$rate_e4", 10000]}, None]},
            "moving_avg_7d": {"$cond": [{"$isNumber": "$avg_e4"}, {"$divide": ["$avg_e4", 10000]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]


def load_department(department: str) -> tuple[list, list]:
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "department not found")
    return [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    d_from, d_to = trend_range(date_from, date_to)
    codes, joined = load_department(department)
    rows = list(db.attendance_logs.aggregate(department_trend_pipeline(codes, joined, d_from, d_to)))
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Admin: explain (executionStats) for each endpoint's main query, built the same way
# --------------------------------------------------------------------------- #
def require(params: dict, names: list) -> None:
    missing = [n for n in names if params.get(n) is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ["query", n], "msg": "Field required", "input": None} for n in missing]
        )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = build_attendance_filter(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        cmd = {"find": collection, "filter": q, "projection": {"_id": 0}, "sort": dict(ATTENDANCE_SORT),
               "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        require({"emp_code": emp_code, "month": month}, ["emp_code", "month"])
        first, last = month_bounds(month)
        collection = "attendance_logs"
        cmd = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, first, last), "cursor": {}}
    elif endpoint == "department_summary":
        require({"month": month}, ["month"])
        first, last = month_bounds(month)
        collection = "employees"
        cmd = {"aggregate": collection, "pipeline": department_summary_pipeline(first, last, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        require({"month": month}, ["month"])
        first, last = month_bounds(month)
        collection = "attendance_logs"
        cmd = {"aggregate": collection,
               "pipeline": late_leaderboard_pipeline(first, last, limit, department), "cursor": {}}
    else:  # department_trend
        require({"department": department, "from": from_, "to": to}, ["department", "from", "to"])
        d_from, d_to = trend_range(from_, to)
        codes, joined = load_department(department)
        collection = "attendance_logs"
        cmd = {"aggregate": collection,
               "pipeline": department_trend_pipeline(codes, joined, d_from, d_to), "cursor": {}}

    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(result))}
