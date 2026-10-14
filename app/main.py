"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    # Every employee code must be unique.
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uniq_employee_code",
    )

    # One attendance record per employee per date.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_attendance_employee_date",
    )

    # Support attendance filtering and date-based sorting.
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )

    # Support department and employee headcount queries.
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="department_joined_on",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def to_utc_seconds(value: int) -> datetime:
    """Validate epoch milliseconds and convert to a UTC datetime."""
    if type(value) is not int or value < 100_000_000_000:
        raise HTTPException(
            status_code=422,
            detail="Timestamp must be integer epoch milliseconds",
        )

    try:
        return datetime.fromtimestamp(value // 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(status_code=422, detail="Timestamp out of range")


def to_ist(dt: datetime) -> datetime:
    """Convert a datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def round_half_up(value: float, places: int = 2) -> float:
    """Round a number using decimal half-up rounding."""
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    )


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: Optional[str] = None,
) -> int:
    """Apply the strict 10-minute grace period."""
    punch_in = to_ist(punch_in)

    shift_day = (
        date.fromisoformat(attendance_date)
        if attendance_date
        else punch_in.date()
    )
    hour, minute = map(int, shift_start.split(":"))
    start = datetime.combine(
        shift_day, time(hour, minute), tzinfo=IST
    )

    elapsed_seconds = (punch_in - start).total_seconds()

    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    """Calculate work hours, rounded half-up to two decimals."""
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: Optional[str] = None,
) -> int:
    """Count overtime only when it reaches at least 30 minutes."""
    hour, minute = map(int, shift_end.split(":"))
    shift_day = date.fromisoformat(date_str)
    end = datetime.combine(
        shift_day, time(hour, minute), tzinfo=IST
    )

    if shift_start is not None:
        start_hour, start_minute = map(int, shift_start.split(":"))
        start_time = time(start_hour, start_minute)

        if time(hour, minute) <= start_time:
            end += timedelta(days=1)

    punch_out = to_ist(punch_out)
    elapsed_seconds = (punch_out - end).total_seconds()

    if elapsed_seconds < 1800:
        return 0

    return int(elapsed_seconds // 60)



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


class AttendancePatchIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

    if department is not None:
        q["department"] = department

    # Count only employees matching the filter.
    total = db.employees.count_documents(q)

    # Page 1 skips 0 records; page 2 skips page_size records.
    skip = (page - 1) * page_size

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # Verify that the employee exists.
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # Validate the requested attendance status.
    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    if body.status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )

    # Convert epoch milliseconds to a UTC datetime.
    if body.punched_at is None:
        ts = datetime.now(timezone.utc)
    else:
        ts = to_utc_seconds(body.punched_at)

    # Determine the punch-in time and date in IST.
    local_ts = to_ist(ts)
    attendance_day = local_ts.date()

    # For overnight shifts, early-morning punches belong
    # to the shift that started on the previous day.
    start_hour, start_minute = map(
        int, emp["shift_start"].split(":")
    )
    end_hour, end_minute = map(
        int, emp["shift_end"].split(":")
    )

    shift_start_time = time(start_hour, start_minute)
    shift_end_time = time(end_hour, end_minute)

    if (
        shift_end_time <= shift_start_time
        and local_ts.time().replace(tzinfo=None) < shift_end_time
    ):
        attendance_day -= timedelta(days=1)

    date_str = attendance_day.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"], date_str
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # The unique compound index prevents duplicate attendance
    # records, including simultaneous punch-in requests.
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )

    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)
    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc)
    else:
        ts = to_utc_seconds(body.punched_at)

    # Find the most recent open attendance record.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        # Check whether a matching record was already punched out.
        previous = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": ts},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )

        if previous is not None:
            raise HTTPException(status_code=409, detail="Already punched out")

        raise HTTPException(status_code=404, detail="No open punch-in record found")


    punch_in_time = record["punch_in"]

    # MongoDB returns naive datetimes in UTC by default.
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)
    else:
        punch_in_time = punch_in_time.astimezone(timezone.utc)


    if ts <= punch_in_time:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    if ts > punch_in_time + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_end"],
        record["date"],
        emp["shift_start"],
    )
    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=409, detail="Attendance already punched out")

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0},
    )
    return updated


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., description="Month in YYYY-MM format"),
):
    # Validate month format.
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Calculate the first and last dates of the month.
    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)

    month_end = next_month - timedelta(days=1)

    joined_on = date.fromisoformat(emp["joined_on"])
    effective_start = max(month_start, joined_on)

    working_days = 0
    current_day = effective_start
    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    # Read this employee's attendance records for the month.
    records = list(
        db.attendance_logs.find(
            {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            },
            {
                "_id": 0,
                "date": 1,
                "status": 1,
                "work_hours": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
            },
        )
    )

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    present_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    for record in records:
        record_date = date.fromisoformat(record["date"])
        status = record.get("status")

        if status == "LEAVE":
            leave_days += 1

        if status in present_statuses:
            if record_date.weekday() < 5:
                present_days += 0.5 if record.get("half_day", False) else 1.0

        late_minutes = record.get("late_minutes", 0) or 0
        if late_minutes > 0:
            late_count += 1
            total_late_minutes += late_minutes

        total_overtime_minutes += record.get("overtime_minutes", 0) or 0

    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., description="Month in YYYY-MM format"),
    department: Optional[str] = Query(None, description="Filter by exact department"),
):
    # Validate month format
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format"
        )

    # Last day of the requested month
    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)

    month_end = next_month - timedelta(days=1)
    month_end_str = month_end.isoformat()
    month_start_str = month_start.isoformat()

    # Count employees who joined by the end of the month
    employee_filter = {"joined_on": {"$lte": month_end_str}}
    if department is not None:
        employee_filter["department"] = department

    employees = list(
        db.employees.find(
            employee_filter,
            {"_id": 0, "emp_code": 1, "department": 1}
        )
    )

    department_codes = {}
    for emp in employees:
        department_codes.setdefault(emp["department"], set()).add(emp["emp_code"])

    # Load only attendance records in the requested month
    attendance_filter = {
        "date": {"$gte": month_start_str, "$lte": month_end_str}
    }
    if department is not None:
        matching_codes = list(department_codes.values())
        emp_codes = set().union(*matching_codes) if matching_codes else set()
        attendance_filter["emp_code"] = {"$in": list(emp_codes)}

    records = list(db.attendance_logs.find(
        attendance_filter,
        {
            "_id": 0,
            "emp_code": 1,
            "date": 1,
            "status": 1,
            "half_day": 1,  # Added this line
            "work_hours": 1,
            "late_minutes": 1,
            "overtime_minutes": 1,
        }
    ))

    employee_departments = {
        emp["emp_code"]: emp["department"] for emp in employees
    }

    summaries = {}

    for dept, codes in department_codes.items():
        summaries[dept] = {
            "department": dept,
            "month": month,
            "headcount": len(codes),
            "present_days": 0.0,
            "work_hours_sum": 0.0,
            "work_hours_count": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        }

    present_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    for record in records:
        emp_code = record["emp_code"]
        dept = employee_departments.get(emp_code)

        # Ignore attendance logs without a matching eligible employee
        if dept not in summaries:
            continue

        summary = summaries[dept]
        record_date = date.fromisoformat(record["date"])
        status = record.get("status")

        if status in present_statuses:
            if record_date.weekday() < 5:
                summary["present_days"] += 0.5 if record.get("half_day", False) else 1.0

            work_hours = record.get("work_hours")
            if work_hours is not None:
                summary["work_hours_sum"] += work_hours
                summary["work_hours_count"] += 1

        if (record.get("late_minutes", 0) or 0) > 0:
            summary["late_count"] += 1

        summary["total_late_minutes"] += record.get("late_minutes", 0) or 0

        if status == "LEAVE":
            summary["leave_count"] += 1

        if status == "ON_DUTY":
            summary["on_duty_count"] += 1

    result = []

    for dept in sorted(summaries):
        summary = summaries[dept]

        # Omit departments with zero headcount
        if summary["headcount"] == 0:
            continue

        count = summary.pop("work_hours_count")
        total_hours = summary.pop("work_hours_sum")

        summary["present_days"] = round_half_up(summary["present_days"], 2)
        summary["avg_work_hours"] = (
            round_half_up(total_hours / count, 2)
            if count > 0 else None
        )

        result.append(summary)

    return result


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., description="Month in YYYY-MM format"),
    department: Optional[str] = Query(None, description="Filter by exact department"),
    limit: int = Query(10, ge=1, le=100),
):
    # Validate month
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    # Get the first and last dates of the month
    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)

    month_end = next_month - timedelta(days=1)

    # Find employees who joined by the end of the requested month
    employee_filter = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_filter["department"] = department

    employees = list(
        db.employees.find(
            employee_filter,
            {"_id": 0, "emp_code": 1, "name": 1, "department": 1},
        )
    )

    employee_info = {
        emp["emp_code"]: {
            "name": emp["name"],
            "department": emp["department"],
        }
        for emp in employees
    }

    if not employee_info:
        return []

    # Get attendance records for the requested month
    attendance_filter = {
        "date": {
            "$gte": month_start.isoformat(),
            "$lte": month_end.isoformat(),
        },
        "emp_code": {"$in": list(employee_info.keys())},
        "late_minutes": {"$gt": 0},
    }

    records = list(
        db.attendance_logs.find(
            attendance_filter,
            {"_id": 0, "emp_code": 1, "late_minutes": 1},
        )
    )

    # Sum late minutes for each employee
    totals = {}
    for record in records:
        emp_code = record["emp_code"]
        totals[emp_code] = (
            totals.get(emp_code, 0) + (record.get("late_minutes", 0) or 0)
        )

    # Sort by total late minutes descending, then employee code ascending
    ranked = sorted(
        totals.items(),
        key=lambda item: (-item[1], item[0]),
    )

    # Assign competition ranks: 1, 2, 2, 4
    result = []
    previous_total = None
    current_rank = 0

    for position, (emp_code, total_late) in enumerate(ranked, start=1):
        if total_late != previous_total:
            current_rank = position
            previous_total = total_late

        result.append({
            "rank": current_rank,
            "emp_code": emp_code,
            "name": employee_info[emp_code]["name"],
            "department": employee_info[emp_code]["department"],
            "total_late_minutes": total_late,
        })

    # Include all tied employees whose rank is within the requested limit
    return [item for item in result if item["rank"] <= limit]



@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    to_date: str = Query(..., description="End date in YYYY-MM-DD format"),
):
    # Validate dates
    try:
        start_day = date.fromisoformat(from_date)
        end_day = date.fromisoformat(to_date)

        if start_day.isoformat() != from_date or end_day.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if end_day < start_day:
        raise HTTPException(
            status_code=422,
            detail="to_date cannot be before from_date",
        )

    if (end_day - start_day).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # Check that the department exists
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(status_code=404, detail="Department not found")

    # MongoDB generates every date in the range.
    # This also handles dates with no attendance records.
    pipeline = [
        {
            "$match": {
                "department": department,
                "joined_on": {"$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": from_date
                                                    }
                                                },
                                                "endDate": {
                                                    "$dateFromString": {
                                                        "dateString": to_date
                                                    }
                                                },
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": from_date
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                },
                "employees": 1,
            }
        },
        {"$unwind": "$days"},
        {
            "$set": {
                "eligible_codes": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$employees",
                                "as": "emp",
                                "cond": {
                                    "$lte": ["$$emp.joined_on", "$days"]
                                },
                            }
                        },
                        "as": "emp",
                        "in": "$$emp.emp_code",
                    }
                },
                "working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$days"
                                }
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$days",
                    "codes": "$eligible_codes",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$days",
                "working_day": 1,
                "headcount": {"$size": "$eligible_codes"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$in": [
                                            "$$log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$multiply": [
                                {"$divide": ["$present_count", "$headcount"]},
                                100,
                            ]
                        },
                        None,
                    ]
                }
            }
        },

        {
            "$set": {
                "date_for_window": {
                    "$dateFromString": {
                        "dateString": "$date"
                    }
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_for_window": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "range": [-6, 0],
                            "unit": "day",
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "date_for_window": 0
            }
        },
        {"$sort": {"date": 1}},
        
    ]

    rows = list(db.employees.aggregate(pipeline))

    # Round attendance rates and calculate the moving average.
    # The date rows themselves are generated by MongoDB above.
    for row in rows:
        rate = row["attendance_rate"]
        if rate is not None:
            row["attendance_rate"] = round_half_up(rate, 4)

        moving_avg = row.get("moving_avg_7d")
        if moving_avg is not None:
            row["moving_avg_7d"] = round_half_up(moving_avg, 4)

    return rows

    # for i, row in enumerate(rows):
    #     recent_rates = [
    #         item["attendance_rate"]
    #         for item in rows[max(0, i - 6):i + 1]
    #         if item["attendance_rate"] is not None
    #     ]

    #     row["moving_avg_7d"] = (
    #         round_half_up(sum(recent_rates) / len(recent_rates), 4)
    #         if recent_rates
    #         else None
    #     )

    # return rows


@app.get("/admin/explain/{endpoint}")
def explain_query(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=100),
):
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of {sorted(allowed_endpoints)}",
        )

    def validate_month(value):
        try:
            parsed = datetime.strptime(value, "%Y-%m").date()
            if parsed.strftime("%Y-%m") != value:
                raise ValueError
            return parsed
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

    def validate_date(value, field_name):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            return parsed
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} must use YYYY-MM-DD format",
            )

    def month_bounds(value):
        first = validate_month(value)
        if first.month == 12:
            next_month = date(first.year + 1, 1, 1)
        else:
            next_month = date(first.year, first.month + 1, 1)
        return first.isoformat(), (next_month - timedelta(days=1)).isoformat()

    def run_find_explain(collection_name, query, projection=None,
                         sort=None, skip=0, result_limit=0):
        find_command = {
            "find": collection_name,
            "filter": query,
        }

        if projection is not None:
            find_command["projection"] = projection
        if sort:
            find_command["sort"] = dict(sort)
        if skip:
            find_command["skip"] = skip
        if result_limit:
            find_command["limit"] = result_limit

        return db.command(
            "explain",
            find_command,
            verbosity="executionStats",
        )

    if endpoint == "attendance_list":
        query = {}

        if emp_code is not None:
            query["emp_code"] = emp_code
        if status is not None:
            query["status"] = status

        if date_from is not None or date_to is not None:
            date_filter = {}

            if date_from is not None:
                validate_date(date_from, "date_from")
                date_filter["$gte"] = date_from

            if date_to is not None:
                validate_date(date_to, "date_to")
                date_filter["$lte"] = date_to

            if date_from is not None and date_to is not None:
                if date_from > date_to:
                    raise HTTPException(
                        status_code=422,
                        detail="date_from cannot be after date_to",
                    )

            query["date"] = date_filter

        return run_find_explain(
            "attendance_logs",
            query,
            projection={"_id": 0},
            sort=[("date", -1), ("emp_code", 1)],
            skip=(page - 1) * page_size,
            result_limit=page_size,
        )

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly",
            )

        month_start, month_end = month_bounds(month)

        return run_find_explain(
            "attendance_logs",
            {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            },
            projection={
                "_id": 0,
                "date": 1,
                "status": 1,
                "work_hours": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
            },
        )

    
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary",
            )

        month_start, month_end = month_bounds(month)

        employee_filter = {"joined_on": {"$lte": month_end}}
        if department is not None:
            employee_filter["department"] = department

        # Explain the employee-selection query.
        employee_explain = run_find_explain(
            "employees",
            employee_filter,
            projection={"_id": 0, "emp_code": 1, "department": 1},
        )

        # Match the actual analytics endpoint's employee selection.
        employees = list(
            db.employees.find(
                employee_filter,
                {"_id": 0, "emp_code": 1, "department": 1},
            )
        )

        # Explain the attendance query used for monthly calculations.
        attendance_filter = {
            "date": {"$gte": month_start, "$lte": month_end}
        }

        if department is not None:
            emp_codes = [emp["emp_code"] for emp in employees]
            attendance_filter["emp_code"] = {"$in": emp_codes}

        attendance_explain = run_find_explain(
            "attendance_logs",
            attendance_filter,
            projection={
                "_id": 0,
                "emp_code": 1,
                "date": 1,
                "status": 1,
                "half_day": 1,
                "work_hours": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
            },
        )

        return {
            "employee_selection": employee_explain,
            "attendance_records": attendance_explain,
        }


    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )

        month_start, month_end = month_bounds(month)

        # Restrict to employees eligible for the requested department/month.
        employee_filter = {"joined_on": {"$lte": month_end}}
        if department is not None:
            employee_filter["department"] = department

        eligible_employees = list(
            db.employees.find(employee_filter, {"_id": 0, "emp_code": 1})
        )
        codes = [emp["emp_code"] for emp in eligible_employees]

        return run_find_explain(
            "attendance_logs",
            {
                "date": {"$gte": month_start, "$lte": month_end},
                "emp_code": {"$in": codes},
                "late_minutes": {"$gt": 0},
            },
            projection={"_id": 0, "emp_code": 1, "late_minutes": 1},
        )

    
    if endpoint == "department_trend":
        start_value = from_date or date_from
        end_value = to_date or date_to

        if not department or not start_value or not end_value:
            raise HTTPException(
                status_code=422,
                detail="department, from_date/date_from, and to_date/date_to are required",
            )

        start_day = validate_date(start_value, "from_date")
        end_day = validate_date(end_value, "to_date")

        if end_day < start_day:
            raise HTTPException(
                status_code=422,
                detail="to_date cannot be before from_date",
            )

        if (end_day - start_day).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        trend_pipeline = [
            {
                "$match": {
                    "department": department,
                    "joined_on": {"$lte": end_value},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "employees": {
                        "$push": {
                            "emp_code": "$emp_code",
                            "joined_on": "$joined_on",
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "days": {
                        "$map": {
                            "input": {
                                "$range": [
                                    0,
                                    {
                                        "$add": [
                                            {
                                                "$dateDiff": {
                                                    "startDate": {
                                                        "$dateFromString": {
                                                            "dateString": start_value
                                                        }
                                                    },
                                                    "endDate": {
                                                        "$dateFromString": {
                                                            "dateString": end_value
                                                        }
                                                    },
                                                    "unit": "day",
                                                }
                                            },
                                            1,
                                        ]
                                    },
                                ]
                            },
                            "as": "offset",
                            "in": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {
                                        "$dateAdd": {
                                            "startDate": {
                                                "$dateFromString": {
                                                    "dateString": start_value
                                                }
                                            },
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                }
                            },
                        }
                    },
                    "employees": 1,
                }
            },
            {"$unwind": "$days"},
            {
                "$set": {
                    "eligible_codes": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$employees",
                                    "as": "emp",
                                    "cond": {
                                        "$lte": ["$$emp.joined_on", "$days"]
                                    },
                                }
                            },
                            "as": "emp",
                            "in": "$$emp.emp_code",
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "day": "$days",
                        "codes": "$eligible_codes",
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$date", "$$day"]},
                                        {"$in": ["$emp_code", "$$codes"]},
                                    ]
                                }
                            }
                        },
                        {
                            "$project": {
                                "_id": 0,
                                "status": 1,
                                "half_day": 1,
                                "late_minutes": 1,
                            }
                        },
                    ],
                    "as": "logs",
                }
            },
        ]

        return db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": trend_pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    raise HTTPException(
        status_code=422,
        detail="Unsupported explain endpoint",
    )



    # Explain the trend's base employee-selection query.
    return db.command(
        "explain",
        {
            "aggregate": "employees",
            "pipeline": [
                {
                    "$match": {
                        "department": department,
                        "joined_on": {"$lte": to_date},
                    }
                }
            ],
            "cursor": {},
        },
        verbosity="executionStats",
    )


@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(
    emp_code: str,
    date: str,
    body: AttendancePatchIn,
):
    # Validate correction details.
    if not body.reason.strip() or not body.regularized_by.strip():
        raise HTTPException(
            status_code=422,
            detail="reason and regularized_by cannot be empty",
        )

    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format",
        )

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    supplied = body.model_fields_set

    if body.status is not None and body.status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    # Convert stored MongoDB timestamps to timezone-aware UTC.
    def as_utc(value):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    old_status = record.get("status")
    old_in = as_utc(record.get("punch_in"))
    old_out = as_utc(record.get("punch_out"))

    new_status = body.status if body.status is not None else old_status
    new_in = (
        to_utc_seconds(body.punch_in)
        if body.punch_in is not None
        else old_in
    )
    new_out = (
        to_utc_seconds(body.punch_out)
        if body.punch_out is not None
        else old_out
    )

    # ABSENT and LEAVE records cannot have punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        if (
            ("punch_in" in supplied and body.punch_in is not None)
            or ("punch_out" in supplied and body.punch_out is not None)
        ):
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot include punch times",
            )
        new_in = None
        new_out = None

    else:
        # A presence status requires a punch-in.
        if new_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires punch_in",
            )

        # Ensure the punch-in belongs to this attendance date.
        local_in = to_ist(new_in)
        start_h, start_m = map(int, emp["shift_start"].split(":"))
        end_h, end_m = map(int, emp["shift_end"].split(":"))
        start_time = time(start_h, start_m)
        end_time = time(end_h, end_m)

        shift_date = local_in.date()
        if (
            end_time <= start_time
            and local_in.time().replace(tzinfo=None) < end_time
        ):
            shift_date -= timedelta(days=1)

        if shift_date.isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in does not belong to this attendance date",
            )

        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in",
                )
            if new_out > new_in + timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

    # Recalculate derived fields.
    if new_in is not None and new_out is not None:
        work_hours = compute_work_hours(new_in, new_out)
        late_minutes = compute_late_minutes(new_in, emp["shift_start"], date)
        overtime_minutes = compute_overtime(
            new_out, emp["shift_end"], date, emp["shift_start"]
        )
        half_day = work_hours < 4.50
    elif new_in is not None:
        work_hours = None
        late_minutes = compute_late_minutes(new_in, emp["shift_start"], date)
        overtime_minutes = 0
        half_day = False
    else:
        work_hours = None
        late_minutes = 0
        overtime_minutes = 0
        half_day = False

    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {}
    for field, new_value in new_values.items():
        old_value = old_values[field]
        if old_value != new_value:
            changes[field] = {"old": old_value, "new": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes to apply")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    
    # Update only if the attendance record has not changed
    # since it was read.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "history": record.get("history", []),
        },
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently. Please retry.",
        )


    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0},
    )
    return updated


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

    if emp_code is not None:
        q["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        date_filter = {}

        if date_from is not None:
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="date_from must use YYYY-MM-DD format",
                )
            date_filter["$gte"] = date_from

        if date_to is not None:
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="date_to must use YYYY-MM-DD format",
                )
            date_filter["$lte"] = date_to

        if (
            date_from is not None
            and date_to is not None
            and date_from > date_to
        ):
            raise HTTPException(
                status_code=422,
                detail="date_from cannot be after date_to",
            )

        q["date"] = date_filter

    if status is not None:
        q["status"] = status

    # Count after applying every filter.
    total = db.attendance_logs.count_documents(q)

    # Sort and paginate in MongoDB, not in Python.
    docs = list(
        db.attendance_logs.find(q, {"_id": 1})
        .sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        )
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    # Fetch the selected records with their full fields.
    ids = [doc["_id"] for doc in docs]

    if not ids:
        items = []
    else:
        selected = db.attendance_logs.find(
            {"_id": {"$in": ids}},
            {"_id": 0},
        )
        by_id = {
            doc.get("emp_code", "") + "|" + doc.get("date", ""): doc
            for doc in selected
        }

        # Preserve the order established by the database query.
        items = [
            by_id[key]
            for key in [
                next(
                    (
                        doc.get("emp_code", "") + "|" + doc.get("date", "")
                        for doc in db.attendance_logs.find(
                            {"_id": item["_id"]},
                            {"emp_code": 1, "date": 1},
                        )
                    ),
                    "",
                )
                for item in docs
            ]
            if key in by_id
        ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
