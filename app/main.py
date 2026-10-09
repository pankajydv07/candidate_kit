"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, model_validator
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Mathematical & Temporal Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[Union[float, Decimal, int]], decimals: int = 2) -> Optional[float]:
    """R8: Round half-up arithmetic using Python stdlib Decimal."""
    if val is None:
        return None
    d = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def truncate_to_seconds(ms: int) -> int:
    """R1: Instants are truncated to whole seconds."""
    return (ms // 1000) * 1000


def ms_to_utc_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to a naive UTC datetime for MongoDB BSON storage."""
    if ms is None:
        return None
    truncated_ms = truncate_to_seconds(ms)
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert MongoDB BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: If shift_end <= shift_start, the shift is overnight."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh, em) <= (sh, sm)


def get_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1: Calculate IST attendance date YYYY-MM-DD.
    Overnight exception: if punch-in IST time is earlier than shift_end,
    it belongs to the shift that started on the previous calendar day.
    """
    punch_in_ist = datetime.fromtimestamp(punch_in_ms / 1000, tz=IST)
    cal_date = punch_in_ist.date()
    if is_overnight_shift(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        if punch_in_ist.time() < time(eh, em):
            cal_date = cal_date - timedelta(days=1)
    return cal_date.isoformat()


def compute_late_minutes(punch_in_ms: int, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Late minutes calculation.
    Grace period is strictly 10 minutes. Late only if strictly > 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_ist = datetime.fromtimestamp(punch_in_ms / 1000, tz=IST)
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(attendance_date_str).replace(
        hour=sh, minute=sm, second=0, microsecond=0, tzinfo=IST
    )
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """R4: Work hours = (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals."""
    sec = (punch_out_ms - punch_in_ms) / 1000
    return round_half_up(sec / 3600, 2)


def compute_overtime(punch_out_ms: int, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    """
    punch_out_ist = datetime.fromtimestamp(punch_out_ms / 1000, tz=IST)
    eh, em = map(int, shift_end.split(":"))
    base_date = datetime.fromisoformat(attendance_date_str)
    if is_overnight_shift(shift_start, shift_end):
        base_date = base_date + timedelta(days=1)
    shift_end_dt = base_date.replace(hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST)
    elapsed_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if elapsed_seconds >= 1800:
        return int(elapsed_seconds // 60)
    return 0


def format_history_entries(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Format history records from MongoDB types to API epoch milliseconds."""
    out = []
    for h in history or []:
        entry = {
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for k, v in (h.get("changes") or {}).items():
            entry["changes"][k] = {
                "from": dt_to_epoch_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
                "to": dt_to_epoch_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to"),
            }
        out.append(entry)
    return out


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB attendance document to OpenAPI contract format."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": format_history_entries(doc.get("history", [])),
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB employee document to OpenAPI contract format."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Lifespan: Startup Idempotent Indexes
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)])
    except Exception:
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Pydantic Models & Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database connection failed")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
    doc = body.model_dump()
    doc["created_at"] = ms_to_utc_dt(now_ms)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    docs = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    items = [format_employee_doc(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_ms = truncate_to_seconds(body.punched_at)
    else:
        punch_in_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))

    attendance_date = get_attendance_date(punch_in_ms, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_ms, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ms_to_utc_dt(punch_in_ms),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_ms = truncate_to_seconds(body.punched_at)
    else:
        punch_out_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_out_dt = ms_to_utc_dt(punch_out_ms)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_ms = dt_to_epoch_ms(rec["punch_in"])
    if punch_out_ms <= punch_in_ms:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if (punch_out_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_ms, punch_out_ms)
    overtime_minutes = compute_overtime(punch_out_ms, emp["shift_start"], emp["shift_end"], rec["date"])
    half_day = bool(work_hours is not None and work_hours < 4.50)

    updated_rec = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not updated_rec:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return format_attendance_doc(updated_rec)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q: Dict[str, Any] = {}
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    if body.status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be supplied with ABSENT or LEAVE")

    old_status = rec["status"]
    old_punch_in_ms = dt_to_epoch_ms(rec.get("punch_in"))
    old_punch_out_ms = dt_to_epoch_ms(rec.get("punch_out"))
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    new_status = body.status if body.status is not None else old_status

    if new_status in ["ABSENT", "LEAVE"]:
        new_punch_in_ms = None
        new_punch_out_ms = None
    else:
        new_punch_in_ms = truncate_to_seconds(body.punch_in) if body.punch_in is not None else old_punch_in_ms
        new_punch_out_ms = truncate_to_seconds(body.punch_out) if body.punch_out is not None else old_punch_out_ms

    if new_status in ["PRESENT", "WFH", "ON_DUTY"]:
        if new_punch_in_ms is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")

    if new_punch_in_ms is not None and new_punch_out_ms is not None:
        if new_punch_out_ms <= new_punch_in_ms:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (new_punch_out_ms - new_punch_in_ms) > 24 * 3600 * 1000:
            raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    if new_punch_in_ms is not None:
        derived_date = get_attendance_date(new_punch_in_ms, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    if new_status in ["ABSENT", "LEAVE"]:
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in_ms, emp["shift_start"], date)
        if new_punch_out_ms is not None:
            new_work_hours = compute_work_hours(new_punch_in_ms, new_punch_out_ms)
            new_overtime_minutes = compute_overtime(new_punch_out_ms, emp["shift_start"], emp["shift_end"], date)
            new_half_day = bool(new_work_hours is not None and new_work_hours < 4.50)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    fields = [
        ("status", old_status, new_status, old_status, new_status),
        ("punch_in", old_punch_in_ms, new_punch_in_ms, ms_to_utc_dt(old_punch_in_ms), ms_to_utc_dt(new_punch_in_ms)),
        ("punch_out", old_punch_out_ms, new_punch_out_ms, ms_to_utc_dt(old_punch_out_ms), ms_to_utc_dt(new_punch_out_ms)),
        ("work_hours", old_work_hours, new_work_hours, old_work_hours, new_work_hours),
        ("late_minutes", old_late_minutes, new_late_minutes, old_late_minutes, new_late_minutes),
        ("overtime_minutes", old_overtime_minutes, new_overtime_minutes, old_overtime_minutes, new_overtime_minutes),
        ("half_day", old_half_day, new_half_day, old_half_day, new_half_day),
    ]

    changes_api = {f: {"from": old_a, "to": new_a} for f, old_a, new_a, _, _ in fields if old_a != new_a}
    changes_db = {f: {"from": old_d, "to": new_d} for f, old_a, new_a, old_d, new_d in fields if old_a != new_a}

    if not changes_api:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
    history_entry = {
        "at": ms_to_utc_dt(now_ms),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": ms_to_utc_dt(new_punch_in_ms),
                "punch_out": ms_to_utc_dt(new_punch_out_ms),
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "work_hours": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    start_date_str = max(month_start, emp["joined_on"])
    working_days = 0
    if start_date_str <= month_end:
        cur = date.fromisoformat(start_date_str)
        total_d = (date.fromisoformat(month_end) - cur).days + 1
        working_days = sum(1 for i in range(total_d) if (cur + timedelta(days=i)).weekday() < 5)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = round_half_up(res.get("present_days", 0.0), 2)
        leave_days = res.get("leave_days", 0)
        late_count = res.get("late_count", 0)
        total_late = res.get("total_late_minutes", 0)
        total_ot = res.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "status": "$logs.status",
                "work_hours": "$logs.work_hours",
                "late_minutes": "$logs.late_minutes",
                "half_day": "$logs.half_day",
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees_set"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": ASCENDING}},
    ]


@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r.get("present_days", 0.0), 2),
            "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
            "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "leave_count": r.get("leave_count", 0),
            "on_duty_count": r.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


def build_leaderboard_pipeline(month_start: str, month_end: str, limit: int, department: Optional[str] = None) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = build_leaderboard_pipeline(month_start, month_end, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "rank": r["rank"],
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        })

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")

    start_d = date.fromisoformat(from_date)
    end_d = date.fromisoformat(to_date)
    days_count = (end_d - start_d).days + 1
    if days_count > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1}))
    if not dept_employees:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    agg_results = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    items = []
    cur = start_d
    rates_window: List[float] = []

    while cur <= end_d:
        d_str = cur.isoformat()
        is_working_day = cur.weekday() < 5
        headcount = sum(1 for e in dept_employees if e.get("joined_on", "") <= d_str)

        log_data = agg_results.get(d_str, {})
        present_count = round_half_up(log_data.get("present_count", 0.0), 2)
        late_count = log_data.get("late_count", 0)

        attendance_rate = None
        if is_working_day and headcount > 0:
            raw_rate = present_count / headcount
            attendance_rate = round_half_up(raw_rate, 4)

        rates_window.append(attendance_rate)
        recent_window = rates_window[-7:]
        valid_rates = [r for r in recent_window if r is not None]

        moving_avg_7d = None
        if valid_rates:
            moving_avg_7d = round_half_up(sum(valid_rates) / len(valid_rates), 4)

        items.append({
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Diagnostic Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
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
        skip = (page - 1) * page_size
        plan = db.command("explain", {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = build_employee_monthly_pipeline(emp_code, f"{month}-01", f"{month}-{num_days:02d}")
        plan = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = build_department_summary_pipeline(f"{month}-01", f"{month}-{num_days:02d}", department)
        plan = db.command("explain", {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = build_leaderboard_pipeline(f"{month}-01", f"{month}-{num_days:02d}", limit, department)
        plan = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        plan = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
