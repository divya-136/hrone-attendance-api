"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from calendar import monthrange
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException,Query
from pydantic import BaseModel
from pymongo import MongoClient

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="emp_code_unique_idx"
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        name="emp_code_date_idx"
    )

    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="date_emp_code_idx"
    )

    db.attendance_logs.create_index(
        [("date", 1), ("late_minutes", 1)],
        name="date_late_minutes_idx"
    )
# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))

    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)

    punch_in_ist = punch_in.astimezone(IST)

    start = punch_in_ist.replace(
        hour=h, minute=m, second=0, microsecond=0
    )

    minutes = int((punch_in_ist - start).total_seconds() // 60)

    return minutes if minutes > 10 else 0



def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)



def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str
) -> int:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    end = datetime.fromisoformat(date_str).replace(
        hour=end_h,
        minute=end_m,
        second=0,
        microsecond=0,
        tzinfo=IST
    )

    if (end_h, end_m) <= (start_h, start_m):
        end += timedelta(days=1)

    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)

    punch_out_ist = punch_out.astimezone(IST)
    minutes = int((punch_out_ist - end).total_seconds() // 60)

    return minutes if minutes >= 30 else 0

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
    punched_at: Optional[int] = None
    status: str = "PRESENT"
   

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
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
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
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
    page_size: int = Query(20, ge=1, le=100)
):

    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        if type(body.punched_at) is not int:
            raise HTTPException(422, "punched_at must be epoch milliseconds")
        ts = datetime.fromtimestamp(
            body.punched_at // 1000, tz=timezone.utc
        ).astimezone(IST)
    else:
        ts = datetime.now(IST)

    ts = ts.replace(microsecond=0)
    d = ts.date().isoformat()
    late_minutes = compute_late_minutes(ts, emp["shift_start"])
    ts = ts.astimezone(timezone.utc).replace(tzinfo=None)

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    db.attendance_logs.insert_one(doc)
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": int(
            doc["punch_in"].replace(tzinfo=timezone.utc).timestamp() * 1000
        ),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        if type(body.punched_at) is not int:
            raise HTTPException(422, "punched_at must be epoch milliseconds")
        ts = datetime.fromtimestamp(
            body.punched_at // 1000, tz=timezone.utc
        )
    else:
        ts = datetime.now(timezone.utc)

    ts = ts.replace(microsecond=0)
    date_str = ts.astimezone(IST).date().isoformat()

    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": date_str
    })
    if record is None:
        raise HTTPException(404, "attendance record not found")
    if record.get("punch_in") is None:
        raise HTTPException(409, "no punch-in recorded")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

        work_hours = compute_work_hours(punch_in_time, ts)
    overtime_minutes = compute_overtime(
        ts, emp["shift_start"], emp["shift_end"], record["date"]
    )
    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts.replace(tzinfo=None),
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }}
    )
    if result.modified_count == 0:
        raise HTTPException(409, "already punched out")

    return {
        "emp_code": body.emp_code,
        "date": record["date"],
        "punch_in": int(punch_in_time.timestamp() * 1000),
        "punch_out": int(ts.timestamp() * 1000),
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }



   

def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
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
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
   
    for d in page_docs:
        d.pop("_id", None)
        for field in ("punch_in", "punch_out"):
            if d.get(field) is not None:
                value = d[field]
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                d[field] = int(value.timestamp() * 1000)

        for entry in d.get("history", []):
            if entry.get("at") is not None:
                value = entry["at"]
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                entry["at"] = int(value.timestamp() * 1000)

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
@app.get("/attendance")


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    from datetime import date as date_type

    try:
        date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })
    if record is None:
        raise HTTPException(404, "attendance record not found")

    if len(body.reason.strip()) < 5 or len(body.reason) > 200:
        raise HTTPException(422, "reason must be 5 to 200 characters")

    if not body.regularized_by.strip() or len(body.regularized_by) > 50:
        raise HTTPException(422, "regularized_by must be 1 to 50 characters")

    if body.status is not None and body.status not in (
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ):
        raise HTTPException(422, "invalid status")

    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")

    def from_ms(value):
        if value is None:
            return None
        if type(value) is not int or not 100000000000 <= value <= 4102444800000:
            raise HTTPException(422, "punch times must be epoch milliseconds")
        return datetime.fromtimestamp(value // 1000, tz=timezone.utc)

    old_status = record.get("status", "PRESENT")
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    new_status = body.status if body.status is not None else old_status
    new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
    new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out

    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None

    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    def as_utc(value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    new_in = as_utc(new_in)
    new_out = as_utc(new_out)

    if new_in is not None and new_in.astimezone(IST).date().isoformat() != date:
        raise HTTPException(422, "punch_in must be on the attendance date")

    if new_out is not None:
        if new_in is None or new_out <= new_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_out - new_in).total_seconds() > 24 * 60 * 60:
            raise HTTPException(422, "punch_out must be within 24 hours")

    if new_in is not None:
        late_minutes = compute_late_minutes(new_in, emp["shift_start"])
    else:
        late_minutes = 0

    if new_in is not None and new_out is not None:
        work_hours = compute_work_hours(new_in, new_out)
        overtime_minutes = compute_overtime(
            new_out, emp["shift_start"], emp["shift_end"], date
        )
        half_day = work_hours < 4.5
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    def ms(value):
        if value is None:
            return None
        return int(as_utc(value).timestamp() * 1000)

    old_values = {
        "status": old_status,
        "punch_in": ms(old_in),
        "punch_out": ms(old_out),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_values = {
        "status": new_status,
        "punch_in": ms(new_in),
        "punch_out": ms(new_out),
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {
        key: {"from": old_values[key], "to": new_values[key]}
        for key in old_values
        if old_values[key] != new_values[key]
    }
    if not changes:
        raise HTTPException(422, "request makes no changes")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason.strip(),
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
            "history": record.get("history", []),
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in.replace(tzinfo=None) if new_in else None,
                "punch_out": new_out.replace(tzinfo=None) if new_out else None,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count == 0:
        raise HTTPException(409, "record changed concurrently; retry")

    updated = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    updated.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        if updated.get(field) is not None:
            value = updated[field]
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    for entry in updated.get("history", []):
        if entry.get("at") is not None:
            value = entry["at"]
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            entry["at"] = int(value.timestamp() * 1000)
        for field in ("punch_in", "punch_out"):
            for change in (entry.get("changes", {}).get(field),):
                if change:
                    for side in ("from", "to"):
                        if change.get(side) is not None:
                            change[side] = int(change[side])

    return updated

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate month format
    try:
        if len(month) != 7 or month[4] != "-":
            raise ValueError

        year = int(month[:4])
        mon = int(month[5:7])

        if not 1 <= mon <= 12:
            raise ValueError

        last_day = monthrange(year, mon)[1]

    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")

    emp = db.employees.find_one({"emp_code": emp_code})

    if emp is None:
        raise HTTPException(404, "employee not found")

    start_date = f"{year:04d}-{mon:02d}-01"
    end_date = f"{year:04d}-{mon:02d}-{last_day:02d}"

    # Calculate working days from the employee's joining date
    effective_start = max(start_date, emp["joined_on"])
    current = datetime.fromisoformat(effective_start).date()
    last = datetime.fromisoformat(end_date).date()

    working_days = 0

    while current <= last:
        if current.weekday() < 5:
            working_days += 1
        current += timedelta(days=1)

    # Get this employee's records for the selected month
    records = list(db.attendance_logs.find({
        "emp_code": emp_code,
        "date": {"$gte": start_date, "$lte": end_date}
    }))

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for record in records:
        status = record.get("status")
        record_date = datetime.fromisoformat(record["date"]).date()

        if status == "LEAVE":
            leave_days += 1

        if record.get("late_minutes", 0) > 0:
            late_count += 1
            total_late_minutes += record.get("late_minutes", 0)

        total_overtime_minutes += record.get("overtime_minutes", 0)

        if status in ("PRESENT", "WFH", "ON_DUTY"):
            if record_date.weekday() < 5:
                present_days += (
                    0.5 if record.get("half_day", False) else 1.0
                )

    # Calculate attendance percentage using half-up rounding
    attendance_pct = None

    if working_days > 0:
        percentage = Decimal(str(present_days * 100 / working_days))
        attendance_pct = float(
            percentage.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }



#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    try:
        year = int(month[:4])
        mon = int(month[5:7])
        last_day = monthrange(year, mon)[1]
        start_date = f"{year:04d}-{mon:02d}-01"
        end_date = f"{year:04d}-{mon:02d}-{last_day:02d}"
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    employee_match = {"joined_on": {"$lte": end_date}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$addFields": {
                "_weekday": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$logs.date"
                                }
                            }
                        },
                        0
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$_weekday", 2]},
                                    {"$lte": ["$_weekday", 6]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}
                                ]
                            },
                            "$logs.work_hours",
                            None
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            "$logs.late_minutes",
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = float(
                Decimal(str(item["avg_work_hours"])).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )
            )

    return {"month": month, "items": items}

#   GET   /analytics/leaderboard/late

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    year = int(month[:4])
    mon = int(month[5:7])
    last_day = monthrange(year, mon)[1]

    start_date = f"{year:04d}-{mon:02d}-01"
    end_date = f"{year:04d}-{mon:02d}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    previous_total = None
    current_rank = 0

    for position, record in enumerate(results, start=1):
        if record["total_late_minutes"] != previous_total:
            current_rank = position
            previous_total = record["total_late_minutes"]

        if current_rank > limit:
            break

        record["rank"] = current_rank
        items.append(record)

    return {"month": month, "items": items}

#   GET   /analytics/departments/{department}/trend

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    from decimal import Decimal, ROUND_HALF_UP

    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "Dates must use YYYY-MM-DD format")

    days = (end - start).days + 1

    if days <= 0 or days > 92:
        raise HTTPException(
            422,
            "The date range must be valid and no longer than 92 days"
        )

    employees = list(db.employees.find({"department": department}))
    if not employees:
        raise HTTPException(404, "Unknown department")

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": from_date,
                    "$lte": to_date
                },
                "emp_code": {
                    "$in": [emp["emp_code"] for emp in employees]
                }
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        }
    ]

    daily_records = {
        item["_id"]: item
        for item in db.attendance_logs.aggregate(pipeline)
    }

    items = []
    rates = []

    current = start
    while current <= end:
        day_string = current.isoformat()
        headcount = sum(
            1 for emp in employees
            if emp["joined_on"] <= day_string
        )
        is_working_day = current.weekday() < 5
        record = daily_records.get(day_string, {})

        present_count = record.get("present_count", 0)
        late_count = record.get("late_count", 0)

        if is_working_day and headcount > 0:
            rate = float(
                (Decimal(str(present_count / headcount))).quantize(
                    Decimal("0.0001"),
                    rounding=ROUND_HALF_UP
                )
            )
        else:
            rate = None

        rates.append(rate)
        window = [value for value in rates[-7:] if value is not None]

        moving_avg = None
        if window:
            moving_avg = float(
                Decimal(str(sum(window) / len(window))).quantize(
                    Decimal("0.0001"),
                    rounding=ROUND_HALF_UP
                )
            )

        items.append({
            "date": day_string,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": moving_avg
        })

        current += timedelta(days=1)

    return {"department": department, "items": items}

#   GET   /admin/explain/{endpoint}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        year, mon = int(month[:4]), int(month[5:7])
        last_day = monthrange(year, mon)[1]
        start_date = f"{year:04d}-{mon:02d}-01"
        end_date = f"{year:04d}-{mon:02d}-{last_day:02d}"
        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": [
                    {
                        "$match": {
                            "emp_code": emp_code,
                            "date": {
                                "$gte": start_date,
                                "$lte": end_date
                            }
                        }
                    }
                ],
                "cursor": {}
            },
            verbosity="executionStats"
        )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        year, mon = int(month[:4]), int(month[5:7])
        last_day = monthrange(year, mon)[1]
        start_date = f"{year:04d}-{mon:02d}-01"
        end_date = f"{year:04d}-{mon:02d}-{last_day:02d}"
        collection = "attendance_logs"
        match = {"date": {"$gte": start_date, "$lte": end_date}}
        if department:
            employees = list(db.employees.find(
                {"department": department}, {"emp_code": 1}
            ))
            match["emp_code"] = {
                "$in": [emp["emp_code"] for emp in employees]
            }
        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": [{"$match": match}],
                "cursor": {}
            },
            verbosity="executionStats"
        )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        year, mon = int(month[:4]), int(month[5:7])
        last_day = monthrange(year, mon)[1]
        start_date = f"{year:04d}-{mon:02d}-01"
        end_date = f"{year:04d}-{mon:02d}-{last_day:02d}"
        match = {
            "date": {"$gte": start_date, "$lte": end_date},
            "late_minutes": {"$gt": 0}
        }
        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": [{"$match": match}],
                "cursor": {}
            },
            verbosity="executionStats"
        )

            
        
    elif endpoint == "attendance_list":
        match = {}
        if emp_code:
            match["emp_code"] = emp_code
        if date_from or date_to:
            match["date"] = {}
            if date_from:
                match["date"]["$gte"] = date_from
            if date_to:
                match["date"]["$lte"] = date_to
        if status:
            match["status"] = status

        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {
                "find": collection,
                "filter": match,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size
            },
            verbosity="executionStats"
        )

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                422, "department, from and to are required"
            )
        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": from_date,
                                "$lte": to_date
                            }
                        }
                    }
                ],
                "cursor": {}
            },
            verbosity="executionStats"
        )

    else:
        raise HTTPException(422, "Unsupported endpoint name")

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain
    }


# --------------------------------for d in page_docs:------------------------------------------- #
