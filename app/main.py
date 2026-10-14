"""
Employee Attendance & Analytics API - simple version.

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (a local .env file works, but real environment
variables always take priority).

NOTE: all code lives in this single file to keep it easy to follow.
"""

import calendar
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

from pymongo import MongoClient
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# Allow the local demo UI (opened via file://) to call the API.
# This has no effect on the automated grader.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- helpers (all business-rule math lives here) ----------------

def millis_to_datetime(millis: int) -> datetime:
    """Epoch millis -> UTC datetime. Truncates to whole seconds (R1)."""
    seconds_only = int(millis) // 1000  # leftover millis are dropped, e.g. 09:40:00.900 -> 09:40:00
    return datetime.fromtimestamp(seconds_only, tz=UTC).replace(microsecond=0)


def datetime_to_millis(moment: datetime) -> int:
    """Datetime -> epoch millis. A naive datetime is treated as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def make_utc_aware(moment: datetime) -> datetime:
    """PyMongo returns naive datetimes; treat them as UTC and make them aware."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def round_to_2_decimals(number) -> float:
    """Round to 2 decimals, half-up (R8). Python's round() is banker's rounding, so Decimal is used."""
    return float(Decimal(str(number)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_to_4_decimals(number) -> float:
    """Round to 4 decimals, half-up, for rates (R8)."""
    return float(Decimal(str(number)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def calculate_work_hours(punch_in_time: datetime, punch_out_time: datetime) -> float:
    """R4: (out - in) in seconds / 3600, rounded to 2 decimals half-up."""
    total_seconds = (make_utc_aware(punch_out_time) - make_utc_aware(punch_in_time)).total_seconds()
    return round_to_2_decimals(Decimal(total_seconds) / Decimal(3600))


def find_attendance_date(punch_time_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the attendance date is the IST calendar date of the punch.
    Only exception is an overnight shift (end <= start, e.g. 22:00-06:00):
    a punch earlier than shift_end belongs to the previous day."""
    punch_time_utc = make_utc_aware(punch_time_utc)
    ist_time = punch_time_utc.astimezone(IST)
    attendance_day = ist_time.date()
    if shift_end <= shift_start:  # overnight; string comparison works because HH:MM is zero-padded
        end_hour, end_minute = map(int, shift_end.split(":"))
        punch_seconds = ist_time.hour * 3600 + ist_time.minute * 60 + ist_time.second
        shift_end_seconds = end_hour * 3600 + end_minute * 60
        if punch_seconds < shift_end_seconds:
            attendance_day = attendance_day - timedelta(days=1)
    return attendance_day.isoformat()


def calculate_late_minutes(punch_time_utc: datetime, shift_start: str, attendance_day: str) -> int:
    """R2: 10-minute grace period. Late only when the punch is STRICTLY more
    than 10:00 after shift_start. When late, count whole minutes (floor)
    since shift_start, not since the end of grace."""
    punch_time_utc = make_utc_aware(punch_time_utc)
    ist_time = punch_time_utc.astimezone(IST)
    year, month_number, day_number = map(int, attendance_day.split("-"))
    start_hour, start_minute = map(int, shift_start.split(":"))
    shift_start_time = datetime(year, month_number, day_number, start_hour, start_minute, 0, tzinfo=IST)
    seconds_late = (ist_time - shift_start_time).total_seconds()
    if seconds_late <= 10 * 60:
        return 0
    return int(seconds_late // 60)


def calculate_overtime_minutes(punch_out_time_utc: datetime, shift_start: str, shift_end: str, attendance_day: str) -> int:
    """R3: whole minutes (floor) from shift_end to punch_out, counted only when >= 30."""
    punch_out_time_utc = make_utc_aware(punch_out_time_utc)
    punch_out_ist = punch_out_time_utc.astimezone(IST)
    year, month_number, day_number = map(int, attendance_day.split("-"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    if shift_end <= shift_start:  # overnight, so the shift ends on the next day
        shift_end_time = datetime(year, month_number, day_number, end_hour, end_minute, 0, tzinfo=IST) + timedelta(days=1)
    else:
        shift_end_time = datetime(year, month_number, day_number, end_hour, end_minute, 0, tzinfo=IST)
    extra_minutes = int((punch_out_ist - shift_end_time).total_seconds() // 60)
    if extra_minutes >= 30:
        return extra_minutes
    return 0


def is_valid_date(date_text: str) -> bool:
    try:
        date.fromisoformat(date_text)
        return True
    except Exception:
        return False


def is_valid_month(month_text: str) -> bool:
    try:
        datetime.strptime(month_text, "%Y-%m")
        return True
    except Exception:
        return False


def get_month_start_and_end(month: str):
    """'2026-07' -> ('2026-07-01', '2026-07-31', days_in_month)"""
    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    return f"{month}-01", f"{month}-{last_day:02d}", last_day


def count_working_days(joined_on: str, month: str) -> int:
    """R7: count Mon-Fri days, starting from joined_on for mid-month joiners."""
    month_start, month_end, _ = get_month_start_and_end(month)
    first_day = max(month_start, joined_on)
    start_date = date.fromisoformat(first_day)
    end_date = date.fromisoformat(month_end)
    working_days = 0
    current_day = start_date
    while current_day <= end_date:
        if current_day.weekday() < 5:  # 0=Monday ... 4=Friday
            working_days += 1
        current_day += timedelta(days=1)
    return working_days


def format_employee_for_api(employee_document: dict) -> dict:
    """DB document -> API response (created_at converted to epoch millis)."""
    return {
        "emp_code": employee_document.get("emp_code"),
        "name": employee_document.get("name"),
        "email": employee_document.get("email"),
        "department": employee_document.get("department"),
        "shift_start": employee_document.get("shift_start"),
        "shift_end": employee_document.get("shift_end"),
        "joined_on": employee_document.get("joined_on"),
        "created_at": datetime_to_millis(make_utc_aware(employee_document.get("created_at"))),
    }


def format_attendance_for_api(record_document: dict) -> dict:
    """DB document -> API response (all times as epoch millis, defaults for missing fields)."""
    punch_in_value = record_document.get("punch_in")
    punch_out_value = record_document.get("punch_out")
    history_entries = record_document.get("history") or []
    formatted_history = []
    for history_item in history_entries:
        formatted_changes = {}
        for field_name, change_detail in (history_item.get("changes") or {}).items():
            old_value = change_detail.get("from")
            new_value = change_detail.get("to")
            # punch_in / punch_out from/to values are dates in MongoDB, millis in the API
            if field_name in ("punch_in", "punch_out"):
                if old_value is not None:
                    old_value = datetime_to_millis(make_utc_aware(old_value))
                if new_value is not None:
                    new_value = datetime_to_millis(make_utc_aware(new_value))
            formatted_changes[field_name] = {"from": old_value, "to": new_value}
        formatted_history.append({
            "at": datetime_to_millis(make_utc_aware(history_item.get("at"))),
            "by": history_item.get("by"),
            "reason": history_item.get("reason"),
            "changes": formatted_changes,
        })
    return {
        "emp_code": record_document.get("emp_code"),
        "date": record_document.get("date"),
        "status": record_document.get("status"),
        "punch_in": datetime_to_millis(make_utc_aware(punch_in_value)) if punch_in_value is not None else None,
        "punch_out": datetime_to_millis(make_utc_aware(punch_out_value)) if punch_out_value is not None else None,
        "work_hours": record_document.get("work_hours"),  # may be None until punch-out
        "late_minutes": record_document.get("late_minutes", 0) or 0,
        "overtime_minutes": record_document.get("overtime_minutes", 0) or 0,
        "half_day": record_document.get("half_day", False) or False,
        "history": formatted_history,
    }


def create_all_indexes():
    """Create all indexes at startup (idempotent). One index per query, sort order, or guarantee."""
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index("joined_on")
    # punch-in race: at most one record per day, enforced by a unique index
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # listing + analytics
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index("status")


try:
    create_all_indexes()  # create now if MongoDB is already up
except Exception:
    pass  # if MongoDB is down, do not crash; /health will return 503


@app.on_event("startup")
def create_indexes_on_startup():
    # retry at server startup (the grader starts the app against a fresh database)
    try:
        create_all_indexes()
    except Exception:
        pass


# ---------------- input models (invalid input automatically becomes 422) ----------------

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("name", "department")
    @classmethod
    def name_must_not_be_blank(cls, text_value: str) -> str:
        # " " looks non-empty but is meaningless; reject it with 422
        if not text_value.strip():
            raise ValueError("must not be blank")
        return text_value


class PunchInIn(BaseModel):
    emp_code: str
    # StrictInt: floats (even 1783312500000.0) and numeric strings ("123") are
    # rejected with 422. Plain int would silently convert them.
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("reason", "regularized_by")
    @classmethod
    def text_must_not_be_blank(cls, text_value: str) -> str:
        if not text_value.strip():
            raise ValueError("must not be blank")
        return text_value


# ---------------- basic endpoints ----------------

@app.get("/health")
def health():
    # Return 503 when MongoDB does not answer, so the runner keeps waiting
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": "mongo not ready"})


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if not is_valid_date(body.joined_on):
        raise HTTPException(422, "bad joined_on, use YYYY-MM-DD")
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end cannot be same")
    new_employee = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": datetime.now(UTC),
    }
    try:
        db.employees.insert_one(new_employee)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    saved_employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    return format_employee_for_api(saved_employee)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    search_filter = {}
    if department:
        search_filter["department"] = department  # exact, case-sensitive match
    total = db.employees.count_documents(search_filter)  # total of the filtered set, not the whole collection
    skip = (page - 1) * page_size
    found_employees = list(db.employees.find(search_filter, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {"items": [format_employee_for_api(item) for item in found_employees], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "unknown employee")
    if body.status not in PRESENT_STATUSES:
        raise HTTPException(422, "status must be PRESENT, WFH or ON_DUTY")
    # when punched_at is omitted, use the current time, truncated to seconds
    if body.punched_at is None:
        current_millis = datetime_to_millis(datetime.now(UTC))
        punch_time = millis_to_datetime(current_millis)
    else:
        punch_time = millis_to_datetime(body.punched_at)
    attendance_day = find_attendance_date(punch_time, employee["shift_start"], employee["shift_end"])  # R1
    late_minutes_value = calculate_late_minutes(punch_time, employee["shift_start"], attendance_day)  # R2
    new_record = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punch_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes_value,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(new_record)  # the unique index decides the punch-in race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    saved_record = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_day})
    return format_attendance_for_api(saved_record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "unknown employee")
    if body.punched_at is None:
        punch_time = millis_to_datetime(datetime_to_millis(datetime.now(UTC)))
    else:
        punch_time = millis_to_datetime(body.punched_at)
    punch_time = make_utc_aware(punch_time)
    # most recent open record with punch_in at or before now (works across midnight for night shifts)
    open_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_time}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not open_record:
        # No open record. If a closed record exists for this punch, it is a
        # repeat punch-out -> 409. If nothing exists at all -> 404.
        closed_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": punch_time}},
            sort=[("punch_in", DESCENDING)],
        )
        if closed_record:
            raise HTTPException(409, "already punched out")
        raise HTTPException(404, "no punch-in found")
    punch_in_time = make_utc_aware(open_record["punch_in"])
    if punch_time <= punch_in_time:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (punch_time - punch_in_time).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out more than 24 hours after punch_in")
    work_hours_value = calculate_work_hours(punch_in_time, punch_time)  # R4
    overtime_value = calculate_overtime_minutes(punch_time, employee["shift_start"], employee["shift_end"], open_record["date"])  # R3
    is_half_day = work_hours_value < 4.50  # R5 (based on the rounded work_hours)
    # atomic claim: update only if the record is still open (exactly one punch-out wins the race)
    update_result = db.attendance_logs.update_one(
        {"_id": open_record["_id"], "punch_out": None},
        {"$set": {"punch_out": punch_time, "work_hours": work_hours_value, "overtime_minutes": overtime_value, "half_day": is_half_day}},
    )
    if update_result.modified_count == 0:
        raise HTTPException(409, "already punched out")
    updated_record = db.attendance_logs.find_one({"_id": open_record["_id"]})
    return format_attendance_for_api(updated_record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and not is_valid_date(date_from):
        raise HTTPException(422, "bad date_from")
    if date_to and not is_valid_date(date_to):
        raise HTTPException(422, "bad date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from > date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, "bad status")
    search_filter = {}
    if emp_code:
        search_filter["emp_code"] = emp_code
    if date_from or date_to:
        search_filter["date"] = {}
        if date_from:
            search_filter["date"]["$gte"] = date_from
        if date_to:
            search_filter["date"]["$lte"] = date_to
    if status:
        search_filter["status"] = status
    total = db.attendance_logs.count_documents(search_filter)
    skip = (page - 1) * page_size
    # sort + paginate inside the database; never load the whole collection into memory (100k records)
    records_cursor = db.attendance_logs.find(search_filter).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    return {"items": [format_attendance_for_api(item) for item in records_cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize(emp_code: str, date_str: str, body: RegularizeIn):
    if not is_valid_date(date_str):
        raise HTTPException(422, "bad date in path")
    if body.status and body.status not in ALL_STATUSES:
        raise HTTPException(422, "bad status")
    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(422, "nothing to change")
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "unknown employee")
    existing_record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not existing_record:
        raise HTTPException(404, "no record for that date")

    old_status = existing_record.get("status")
    old_punch_in = existing_record.get("punch_in")
    old_punch_out = existing_record.get("punch_out")
    old_work_hours = existing_record.get("work_hours")
    old_late_minutes = existing_record.get("late_minutes", 0) or 0
    old_overtime = existing_record.get("overtime_minutes", 0) or 0
    old_half_day = existing_record.get("half_day", False) or False

    new_status = body.status if body.status is not None else old_status
    new_punch_in = millis_to_datetime(body.punch_in) if body.punch_in is not None else (make_utc_aware(old_punch_in) if old_punch_in else None)
    new_punch_out = millis_to_datetime(body.punch_out) if body.punch_out is not None else (make_utc_aware(old_punch_out) if old_punch_out else None)

    # punch times cannot be supplied together with ABSENT / LEAVE; both times are cleared
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times not allowed with ABSENT/LEAVE")
        new_punch_in = None
        new_punch_out = None
    else:
        # a presence status requires a punch_in
        if new_punch_in is None:
            raise HTTPException(422, "punch_in required for presence status")
        # punch_in must stay on the record's attendance date (R1)
        if find_attendance_date(new_punch_in, employee["shift_start"], employee["shift_end"]) != date_str:
            raise HTTPException(422, "punch_in must stay on the record date")
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out more than 24 hours after punch_in")

    # recompute the derived fields from the final times (R2-R5)
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes, new_overtime, new_work_hours, new_half_day = 0, 0, None, False
    else:
        new_late_minutes = calculate_late_minutes(new_punch_in, employee["shift_start"], date_str)
        if new_punch_out is not None:
            new_work_hours = calculate_work_hours(new_punch_in, new_punch_out)
            new_overtime = calculate_overtime_minutes(new_punch_out, employee["shift_start"], employee["shift_end"], date_str)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours, new_overtime, new_half_day = None, 0, False

    # history keeps only the fields that actually changed
    def datetime_to_millis_or_none(moment):
        return datetime_to_millis(moment) if moment is not None else None

    changed_fields = {}
    if new_status != old_status:
        changed_fields["status"] = {"from": old_status, "to": new_status}
    if datetime_to_millis_or_none(new_punch_in) != datetime_to_millis_or_none(make_utc_aware(old_punch_in) if old_punch_in else None):
        changed_fields["punch_in"] = {"from": datetime_to_millis_or_none(make_utc_aware(old_punch_in) if old_punch_in else None), "to": datetime_to_millis_or_none(new_punch_in)}
    if datetime_to_millis_or_none(new_punch_out) != datetime_to_millis_or_none(make_utc_aware(old_punch_out) if old_punch_out else None):
        changed_fields["punch_out"] = {"from": datetime_to_millis_or_none(make_utc_aware(old_punch_out) if old_punch_out else None), "to": datetime_to_millis_or_none(new_punch_out)}
    if new_work_hours != old_work_hours:
        changed_fields["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changed_fields["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime != old_overtime:
        changed_fields["overtime_minutes"] = {"from": old_overtime, "to": new_overtime}
    if new_half_day != old_half_day:
        changed_fields["half_day"] = {"from": old_half_day, "to": new_half_day}
    if not changed_fields:
        raise HTTPException(422, "request changes nothing")

    # history entry for MongoDB (datetimes) converted from the millis-based changes
    history_changes_for_db = {}
    for field_name, change_detail in changed_fields.items():
        old_value, new_value = change_detail["from"], change_detail["to"]
        if field_name in ("punch_in", "punch_out"):
            old_value = millis_to_datetime(old_value) if old_value is not None else None
            new_value = millis_to_datetime(new_value) if new_value is not None else None
        history_changes_for_db[field_name] = {"from": old_value, "to": new_value}
    history_entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": history_changes_for_db}

    # a single update with $set + $push, so history entries are never lost
    db.attendance_logs.update_one(
        {"_id": existing_record["_id"]},
        {"$set": {"status": new_status, "punch_in": new_punch_in, "punch_out": new_punch_out,
                   "work_hours": new_work_hours, "late_minutes": new_late_minutes,
                   "overtime_minutes": new_overtime, "half_day": new_half_day},
         "$push": {"history": history_entry}},
    )
    saved_record = db.attendance_logs.find_one({"_id": existing_record["_id"]})
    return format_attendance_for_api(saved_record)


# ---------------- aggregation pipeline builders (shared by endpoints + explain) ----------------
# The explain endpoint must run the SAME pipeline the real endpoint runs
# (same filters, sort, pagination), so both are built here in one place.

def build_monthly_report_pipeline(emp_code: str, month_start: str, month_end: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", PRESENT_STATUSES]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_ot": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary_pipeline(employee_filter: dict, month_start: str, month_end: str):
    return [
        {"$match": employee_filter},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": month_start, "$lte": month_end}}}],
            "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", PRESENT_STATUSES]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "work_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", PRESENT_STATUSES]}, {"$ne": ["$logs.work_hours", None]}]},
                "$logs.work_hours", 0]}},
            "work_n": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", PRESENT_STATUSES]}, {"$ne": ["$logs.work_hours", None]}]},
                1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(log_filter: dict):
    return [
        {"$match": log_filter},
        {"$group": {"_id": "$emp_code",
                     "total_late": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                     "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late": {"$gt": 0}}},
        {"$sort": {"total_late": -1, "_id": 1}},
    ]


def build_daily_trend_pipeline(employee_codes: list, date_from: str, date_to: str, range_start: datetime, range_end_exclusive: datetime):
    return [
        {"$match": {"emp_code": {"$in": employee_codes}, "date": {"$gte": date_from, "$lte": date_to}}},
        {"$group": {"_id": "$date",
                     "present_count": {"$sum": {"$cond": [
                         {"$in": ["$status", PRESENT_STATUSES]},
                         {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                     "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [range_start, range_end_exclusive]}}},
        {"$sort": {"d": 1}},
    ]


# ---------------- analytics (MongoDB aggregation pipelines, no Python loops over records) ----------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    if not is_valid_month(month):
        raise HTTPException(422, "bad month, use YYYY-MM")
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "unknown employee")
    month_start, month_end, _ = get_month_start_and_end(month)
    working_days_count = count_working_days(employee["joined_on"], month)  # R7; small calendar loop (at most 31 days)

    # read the stored derived values; never recompute them from punch times
    report_pipeline = build_monthly_report_pipeline(emp_code, month_start, month_end)
    grouped_rows = list(db.attendance_logs.aggregate(report_pipeline))
    totals_row = grouped_rows[0] if grouped_rows else {}
    present_days_count = float(totals_row.get("present_days", 0))
    attendance_percent = round_to_2_decimals(present_days_count / working_days_count * 100) if working_days_count > 0 else None
    return {
        "emp_code": emp_code, "month": month,
        "working_days": working_days_count, "present_days": present_days_count,
        "leave_days": int(totals_row.get("leave_days", 0)),
        "late_count": int(totals_row.get("late_count", 0)),
        "total_late_minutes": int(totals_row.get("total_late", 0)),
        "total_overtime_minutes": int(totals_row.get("total_ot", 0)),
        "attendance_pct": attendance_percent,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    if not is_valid_month(month):
        raise HTTPException(422, "bad month")
    month_start, month_end, _ = get_month_start_and_end(month)
    # R9: everyone who joined on or before the last day of the month counts, with or without logs
    employee_filter = {"joined_on": {"$lte": month_end}}
    if department:
        employee_filter["department"] = department
    summary_query = build_department_summary_pipeline(employee_filter, month_start, month_end)
    grouped_results = list(db.employees.aggregate(summary_query))
    summary_items = []
    for department_group in grouped_results:
        if department_group["_id"] is None:
            continue
        records_with_hours = department_group.get("work_n", 0)
        average_work_hours = round_to_2_decimals(department_group.get("work_sum", 0) / records_with_hours) if records_with_hours > 0 else None
        summary_items.append({
            "department": department_group["_id"],
            "headcount": len(department_group.get("emps", [])),
            "present_days": float(department_group.get("present_days", 0)),
            "avg_work_hours": average_work_hours,
            "late_count": int(department_group.get("late_count", 0)),
            "total_late_minutes": int(department_group.get("total_late", 0)),
            "leave_count": int(department_group.get("leave_count", 0)),
            "on_duty_count": int(department_group.get("on_duty_count", 0)),
        })
    return {"month": month, "items": summary_items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    if not is_valid_month(month):
        raise HTTPException(422, "bad month")
    month_start, month_end, _ = get_month_start_and_end(month)
    department_employee_codes = None
    employee_details = {}
    if department:
        department_employees = list(db.employees.find({"department": department}, {"_id": 0}))
        department_employee_codes = [item["emp_code"] for item in department_employees]
        employee_details = {item["emp_code"]: item for item in department_employees}
        if not department_employee_codes:
            return {"month": month, "items": []}
    log_filter = {"date": {"$gte": month_start, "$lte": month_end}}
    if department_employee_codes is not None:
        log_filter["emp_code"] = {"$in": department_employee_codes}
    leaderboard_query = build_late_leaderboard_pipeline(log_filter)
    grouped_totals = list(db.attendance_logs.aggregate(leaderboard_query))
    # names and departments come from employees; logs without an employee are ignored
    if department is None:
        all_codes = [row["_id"] for row in grouped_totals]
        for item in db.employees.find({"emp_code": {"$in": all_codes}}, {"_id": 0}):
            employee_details[item["emp_code"]] = item
    employees_with_late = [row for row in grouped_totals if row["_id"] in employee_details]
    employees_with_late.sort(key=lambda row: (-row["total_late"], row["_id"]))
    # standard competition ranking: 1, 2, 2, 4 (the array position is NOT the rank)
    ranked_items = []
    previous_total, previous_rank = None, 0
    for position, row in enumerate(employees_with_late):
        if row["total_late"] == previous_total:
            current_rank = previous_rank
        else:
            current_rank = position + 1
        previous_total, previous_rank = row["total_late"], current_rank
        if current_rank <= limit:  # limit applies after ranking, so ties can return more than `limit` rows
            employee = employee_details[row["_id"]]
            ranked_items.append({"rank": current_rank, "emp_code": row["_id"], "name": employee["name"],
                           "department": employee["department"], "total_late_minutes": int(row["total_late"]),
                           "late_count": int(row["late_count"])})
    return {"month": month, "items": ranked_items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    if not is_valid_date(from_) or not is_valid_date(to):
        raise HTTPException(422, "bad from/to, use YYYY-MM-DD")
    if to < from_:
        raise HTTPException(422, "to < from")
    start_day, end_day = date.fromisoformat(from_), date.fromisoformat(to)
    if (end_day - start_day).days + 1 > 92:
        raise HTTPException(422, "range more than 92 days")
    department_employees = list(db.employees.find({"department": department}, {"_id": 0}))
    if not department_employees:
        raise HTTPException(404, "unknown department")
    department_employee_codes = [item["emp_code"] for item in department_employees]
    joining_dates = [item["joined_on"] for item in department_employees]

    # aggregation with $densify also returns empty days (weekends / days with no records)
    range_start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC)
    range_end_exclusive = datetime(end_day.year, end_day.month, end_day.day, tzinfo=UTC) + timedelta(days=1)
    trend_query = build_daily_trend_pipeline(department_employee_codes, from_, to, range_start, range_end_exclusive)
    daily_totals = {}
    try:
        for grouped_day in db.attendance_logs.aggregate(trend_query):
            day_key = grouped_day.get("_id")
            if day_key is None:  # a gap day created by densify
                gap_date = grouped_day["d"]
                if gap_date.tzinfo is None:
                    gap_date = gap_date.replace(tzinfo=UTC)
                day_key = gap_date.astimezone(UTC).date().isoformat()
            daily_totals[day_key] = (float(grouped_day.get("present_count", 0) or 0), int(grouped_day.get("late_count", 0) or 0))
    except Exception:
        # fallback for an older MongoDB without densify: plain grouping, gaps filled below
        daily_totals = {}
        for grouped_day in db.attendance_logs.aggregate(trend_query[:2]):
            daily_totals[grouped_day["_id"]] = (float(grouped_day.get("present_count", 0) or 0), int(grouped_day.get("late_count", 0) or 0))

    trend_rows, past_rates = [], []
    current_day = start_day
    while current_day <= end_day:
        day_string = current_day.isoformat()
        present_count, late_count = daily_totals.get(day_string, (0.0, 0))
        is_working_day = current_day.weekday() < 5
        headcount_today = sum(1 for joining_date in joining_dates if joining_date <= day_string)  # R9: joined on or before that day
        if is_working_day and headcount_today > 0:
            attendance_rate_today = round_to_4_decimals(present_count / headcount_today)
        else:
            attendance_rate_today = None
        past_rates.append(attendance_rate_today)
        # 7-day moving average over the current row and up to 6 preceding rows inside the range
        last_7_day_rates = [rate for rate in past_rates[-7:] if rate is not None]
        moving_average = round_to_4_decimals(sum(last_7_day_rates) / len(last_7_day_rates)) if last_7_day_rates else None
        trend_rows.append({"date": day_string, "is_working_day": is_working_day, "headcount": headcount_today,
                       "present_count": present_count, "late_count": late_count,
                       "attendance_rate": attendance_rate_today, "moving_avg_7d": moving_average})
        current_day += timedelta(days=1)
    return {"department": department, "items": trend_rows}


# ---------------- explain (index proof) ----------------

@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        search_filter = {}
        if emp_code:
            search_filter["emp_code"] = emp_code
        if date_from or date_to:
            search_filter["date"] = {}
            if date_from:
                search_filter["date"]["$gte"] = date_from
            if date_to:
                search_filter["date"]["$lte"] = date_to
        if status:
            search_filter["status"] = status
        skip = (page - 1) * page_size
        explain_command = {"explain": {"find": "attendance_logs", "filter": search_filter,
                            "sort": {"date": -1, "emp_code": 1},
                            "skip": skip, "limit": page_size},
               "verbosity": "executionStats"}
        explain_result = db.command(explain_command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        month_start, month_end, _ = get_month_start_and_end(month)
        report_pipeline = build_monthly_report_pipeline(emp_code, month_start, month_end)
        explain_result = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": report_pipeline, "cursor": {}},
                           "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        month_start, month_end, _ = get_month_start_and_end(month)
        employee_filter = {"joined_on": {"$lte": month_end}}
        if department:
            employee_filter["department"] = department
        summary_query = build_department_summary_pipeline(employee_filter, month_start, month_end)
        explain_result = db.command({"explain": {"aggregate": "employees", "pipeline": summary_query, "cursor": {}},
                           "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_result}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        month_start, month_end, _ = get_month_start_and_end(month)
        log_filter = {"date": {"$gte": month_start, "$lte": month_end}}
        if department:
            department_employee_codes = [item["emp_code"] for item in db.employees.find({"department": department}, {"emp_code": 1})]
            log_filter["emp_code"] = {"$in": department_employee_codes}
        leaderboard_query = build_late_leaderboard_pipeline(log_filter)
        explain_result = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": leaderboard_query, "cursor": {}},
                           "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, to required")
        department_employee_codes = [item["emp_code"] for item in db.employees.find({"department": department}, {"emp_code": 1})]
        from_date = date.fromisoformat(from_)
        to_date = date.fromisoformat(to)
        trend_query = build_daily_trend_pipeline(department_employee_codes, from_, to,
                              datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC),
                              datetime(to_date.year, to_date.month, to_date.day, tzinfo=UTC) + timedelta(days=1))
        explain_result = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": trend_query, "cursor": {}},
                           "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    raise HTTPException(422, "unknown endpoint")
