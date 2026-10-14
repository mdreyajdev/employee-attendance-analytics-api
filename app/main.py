"""Employee Attendance & Analytics API. Run: uvicorn app.main:app --port 8000"""
import os, re, math
import json
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson import json_util
from pymongo.errors import DuplicateKeyError
load_dotenv()
client = MongoClient(os.getenv('MONGO_URI', 'mongodb://localhost:27017'), tz_aware=True)
db = client[os.getenv('MONGO_DB', 'attendance_db')]
employees, logs = (db.employees, db.attendance_logs)
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ['PRESENT', 'WFH', 'ON_DUTY']
STATUSES = ['PRESENT', 'ABSENT', 'LEAVE', 'WFH', 'ON_DUTY']
app = FastAPI(title='Employee Attendance & Analytics API', version='2.0.0')

def now():
    return datetime.now(timezone.utc).replace(microsecond=0)

def to_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def from_ms(v):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 100000000000 or (v > 253402300799999):
        raise HTTPException(422, 'Timestamp must be integer epoch milliseconds')
    try:
        return datetime.fromtimestamp(v / 1000, timezone.utc).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, 'Timestamp out of range')

def utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

def api(doc):
    if not doc:
        return doc
    x = dict(doc)
    x.pop('_id', None)
    for k in ('created_at', 'punch_in', 'punch_out'):
        if k in x:
            x[k] = to_ms(x[k])
    x.setdefault('late_minutes', 0)
    x.setdefault('overtime_minutes', 0)
    x.setdefault('half_day', False)
    x.setdefault('history', [])
    for h in x['history']:
        if 'at' in h:
            h['at'] = to_ms(h['at'])
        for change in h.get('changes', {}).values():
            if isinstance(change, dict):
                for k in ('from', 'to'):
                    if isinstance(change.get(k), datetime):
                        change[k] = to_ms(change[k])
    return x

def parse_date(s):
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s:
            raise ValueError()
        return d
    except Exception:
        raise HTTPException(422, 'Date must be YYYY-MM-DD')

def parse_month(s):
    if not re.fullmatch('\\d{4}-\\d{2}', s or ''):
        raise HTTPException(422, 'month must be YYYY-MM')
    try:
        y, m = map(int, s.split('-'))
        if not 1 <= m <= 12:
            raise ValueError()
        a = date(y, m, 1)
        b = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        return (a, b)
    except ValueError:
        raise HTTPException(422, 'month must be YYYY-MM')

def tm(s):
    if not isinstance(s, str) or not re.fullmatch('(?:[01]\\d|2[0-3]):[0-5]\\d', s):
        raise HTTPException(422, 'Time must be HH:MM')
    h, m = map(int, s.split(':'))
    return h * 60 + m

def rnd(v, n=2):
    return None if v is None else float(Decimal(str(v)).quantize(Decimal('1').scaleb(-n), rounding=ROUND_HALF_UP))

def shift_bounds(day, emp):
    d = parse_date(day)
    a = tm(emp['shift_start'])
    b = tm(emp['shift_end'])
    start = datetime(d.year, d.month, d.day, a // 60, a % 60, tzinfo=IST)
    endday = d + timedelta(days=1) if b <= a else d
    end = datetime(endday.year, endday.month, endday.day, b // 60, b % 60, tzinfo=IST)
    return (start.astimezone(timezone.utc), end.astimezone(timezone.utc))

def att_day(ts, emp):
    local = utc(ts).astimezone(IST)
    start = tm(emp['shift_start'])
    end = tm(emp['shift_end'])
    d = local.date()
    if end <= start and local.hour * 60 + local.minute < end:
        d -= timedelta(days=1)
    return d.isoformat()

def derived(pin, pout, status, emp, day):
    if status not in PRESENCE:
        return dict(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    if pin is None:
        raise HTTPException(422, 'Presence status requires punch_in')
    pin = utc(pin).replace(microsecond=0)
    if att_day(pin, emp) != day:
        raise HTTPException(422, 'punch_in must belong to attendance date')
    start, end = shift_bounds(day, emp)
    elapsed_seconds = (pin - start).total_seconds()
    late = (
        math.floor(elapsed_seconds / 60)
        if elapsed_seconds > 600
        else 0
    )
    if pout is None:
        return dict(punch_in=pin, punch_out=None, work_hours=None, late_minutes=late, overtime_minutes=0, half_day=False)
    pout = utc(pout).replace(microsecond=0)
    secs = (pout - pin).total_seconds()
    if secs <= 0 or secs > 86400:
        raise HTTPException(422, 'punch_out must be after punch_in and within 24 hours')
    hours = rnd(secs / 3600)
    over = math.floor((pout - end).total_seconds() / 60)
    return dict(punch_in=pin, punch_out=pout, work_hours=hours, late_minutes=late, overtime_minutes=max(0, over) if over >= 30 else 0, half_day=hours < 4.5)

def weekdays(a, b):
    n = (b - a).days
    weeks, rem = divmod(max(n, 0), 7)
    total = weeks * 5
    for i in range(rem):
        if (a.weekday() + i) % 7 < 5:
            total += 1
    return total

def indexes():
    employees.create_index([('emp_code', ASCENDING)], unique=True, name='uq_emp_code')
    employees.create_index([('department', ASCENDING), ('joined_on', ASCENDING)], name='ix_dept_joined')
    logs.create_index([('emp_code', ASCENDING), ('date', ASCENDING)], unique=True, name='uq_emp_date')
    logs.create_index([('date', DESCENDING), ('emp_code', ASCENDING)], name='ix_date_emp')
    logs.create_index([('emp_code', ASCENDING), ('punch_in', DESCENDING)], name='ix_emp_punch')
    logs.create_index([('status', ASCENDING), ('date', ASCENDING)], name='ix_status_date')

@app.on_event('startup')
def startup():
    indexes()

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra='ignore')
    emp_code: str
    name: str = Field(min_length=1, max_length=120)
    email: str
    department: str = Field(min_length=1, max_length=100)
    shift_start: str = '09:30'
    shift_end: str = '18:30'
    joined_on: str

    @field_validator('emp_code')
    @classmethod
    def code(cls, v):
        if not re.fullmatch('EMP\\d{4,6}', v):
            raise ValueError('emp_code must match EMP followed by 4-6 digits')
        return v

    @field_validator('shift_start', 'shift_end')
    @classmethod
    def shift(cls, v):
        tm(v)
        return v

    @field_validator('joined_on')
    @classmethod
    def joined(cls, v):
        parse_date(v)
        return v

class PunchIn(BaseModel):
    model_config = ConfigDict(extra='ignore')
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal['PRESENT', 'WFH', 'ON_DUTY'] = 'PRESENT'

    @field_validator('punched_at')
    @classmethod
    def ts(cls, v):
        if v is not None:
            from_ms(v)
        return v

class PunchOut(BaseModel):
    model_config = ConfigDict(extra='ignore')
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator('punched_at')
    @classmethod
    def ts(cls, v):
        if v is not None:
            from_ms(v)
        return v

class Regularize(BaseModel):
    model_config = ConfigDict(extra='ignore')
    status: Optional[Literal['PRESENT', 'ABSENT', 'LEAVE', 'WFH', 'ON_DUTY']] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator('punch_in', 'punch_out')
    @classmethod
    def ts(cls, v):
        if v is not None:
            from_ms(v)
        return v

@app.get('/health')
def health():
    try:
        client.admin.command('ping')
        return {'status': 'ok'}
    except Exception:
        raise HTTPException(503, 'Database unavailable')

@app.post('/employees', status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc['created_at'] = now()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'emp_code already exists')
    return api(doc)

@app.get('/employees')
def list_employees(department: Optional[str]=None, page: int=Query(1, ge=1), page_size: int=Query(20, ge=1, le=100)):
    q = {'department': department} if department is not None else {}
    return {'items': [api(x) for x in employees.find(q, {'_id': 0}).sort('emp_code', 1).skip((page - 1) * page_size).limit(page_size)], 'total': employees.count_documents(q), 'page': page, 'page_size': page_size}

@app.post('/attendance/punch-in', status_code=201)
def punch_in(body: PunchIn):
    emp = employees.find_one({'emp_code': body.emp_code})
    if not emp:
        raise HTTPException(404, 'Employee not found')
    ts = (from_ms(body.punched_at) if body.punched_at is not None else now()).replace(microsecond=0)
    day = att_day(ts, emp)
    doc = {'emp_code': body.emp_code, 'date': day, 'status': body.status, **derived(ts, None, body.status, emp, day), 'history': []}
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'Already punched in for this attendance date')
    return api(doc)

@app.post('/attendance/punch-out')
def punch_out(body: PunchOut):
    emp = employees.find_one({'emp_code': body.emp_code})
    if not emp:
        raise HTTPException(404, 'Employee not found')
    ts = (from_ms(body.punched_at) if body.punched_at is not None else now()).replace(microsecond=0)
    rec = logs.find_one({'emp_code': body.emp_code, 'punch_in': {'$lte': ts}, 'punch_out': None, 'status': {'$in': PRESENCE}}, sort=[('punch_in', DESCENDING)])
    if not rec:
        closed = logs.find_one({'emp_code': body.emp_code, 'punch_in': {'$lte': ts}, 'punch_out': {'$ne': None}, 'status': {'$in': PRESENCE}}, sort=[('punch_in', DESCENDING)])
        if closed:
            raise HTTPException(409, 'Attendance record already closed')
        raise HTTPException(404, 'No open punch-in record found')
    d = derived(rec['punch_in'], ts, rec['status'], emp, rec['date'])
    upd = logs.update_one({'_id': rec['_id'], 'punch_out': None}, {'$set': d})
    if upd.modified_count != 1:
        raise HTTPException(409, 'Attendance record changed concurrently')
    return api(logs.find_one({'_id': rec['_id']}))

@app.get('/attendance')
def list_attendance(emp_code: Optional[str]=None, date_from: Optional[str]=None, date_to: Optional[str]=None, status: Optional[Literal['PRESENT', 'ABSENT', 'LEAVE', 'WFH', 'ON_DUTY']]=None, page: int=Query(1, ge=1), page_size: int=Query(20, ge=1, le=100)):
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and (date_from > date_to):
        raise HTTPException(422, 'date_from must be <= date_to')
    q = {}
    if emp_code is not None:
        q['emp_code'] = emp_code
    if status is not None:
        q['status'] = status
    if date_from or date_to:
        q['date'] = {}
        if date_from:
            q['date']['$gte'] = date_from
        if date_to:
            q['date']['$lte'] = date_to
    docs = logs.find(q).sort([('date', DESCENDING), ('emp_code', ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {'items': [api(x) for x in docs], 'total': logs.count_documents(q), 'page': page, 'page_size': page_size}

@app.patch('/attendance/{emp_code}/{date}')
def patch_attendance(emp_code: str, date: str, body: Regularize):
    parse_date(date)
    emp = employees.find_one({'emp_code': emp_code})
    rec = logs.find_one({'emp_code': emp_code, 'date': date})
    if not emp or not rec:
        raise HTTPException(404, 'Employee or attendance record not found')
    fields = body.model_dump(exclude_unset=True)
    reason = fields.pop('reason')
    by = fields.pop('regularized_by')
    status = fields.get('status', rec.get('status'))
    if status in ('ABSENT', 'LEAVE'):
        if fields.get('punch_in') is not None or fields.get('punch_out') is not None:
            raise HTTPException(422, 'ABSENT/LEAVE cannot have punch times')
        pin = pout = None
    else:
        pi = fields.get('punch_in', to_ms(rec.get('punch_in')))
        po = fields.get('punch_out', to_ms(rec.get('punch_out')))
        if pi is None:
            raise HTTPException(422, 'Presence status requires punch_in')
        pin = from_ms(pi)
        pout = from_ms(po)
    new = {'status': status, **derived(pin, pout, status, emp, date)}
    changes = {}
    for k, v in new.items():
        old = rec.get(k)
        if k in ('late_minutes', 'overtime_minutes'):
            old = old or 0
        if k == 'half_day':
            old = bool(old)
        if k in ('punch_in', 'punch_out'):
            old = utc(old) if old else None
            v = utc(v) if v else None
        if old != v:
            changes[k] = {'from': old, 'to': v}
    if not changes:
        raise HTTPException(422, 'Request does not change the record')
    hist = rec.get('history', [])
    res = logs.update_one({'_id': rec['_id'], 'history': hist}, {'$set': new, '$push': {'history': {'at': now(), 'by': by, 'reason': reason, 'changes': changes}}})
    if res.modified_count != 1:
        raise HTTPException(409, 'Record changed concurrently; retry')
    return api(logs.find_one({'_id': rec['_id']}))

@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code: str, month: str):
    a, b = parse_month(month)
    emp = employees.find_one({'emp_code': emp_code})
    if not emp:
        raise HTTPException(404, 'Employee not found')
    joined = max(a, parse_date(emp['joined_on']))
    wd = weekdays(joined, b)
    pipe = [{'$match': {'emp_code': emp_code, 'date': {'$gte': a.isoformat(), '$lt': b.isoformat()}}}, {'$addFields': {'dow': {'$dayOfWeek': {'$dateFromString': {'dateString': '$date'}}}}}, {'$group': {'_id': None, 'present_days': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', PRESENCE]}, {'$in': ['$dow', [2, 3, 4, 5, 6]]}]}, {'$cond': [{'$eq': ['$half_day', True]}, 0.5, 1]}, 0]}}, 'leave_days': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}, 'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}}, 'total_overtime_minutes': {'$sum': {'$ifNull': ['$overtime_minutes', 0]}}}}]
    rows = list(logs.aggregate(pipe))
    r = rows[0] if rows else {}
    pd = r.get('present_days', 0)
    return {'emp_code': emp_code, 'month': month, 'working_days': wd, 'present_days': rnd(pd), 'leave_days': r.get('leave_days', 0), 'late_count': r.get('late_count', 0), 'total_late_minutes': r.get('total_late_minutes', 0), 'total_overtime_minutes': r.get('total_overtime_minutes', 0), 'attendance_pct': rnd(pd / wd * 100) if wd else None}

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    a, b = parse_month(month)

    match = {
        "joined_on": {"$lt": b.isoformat()}
    }

    if department is not None:
        match["department"] = department

    pipe = [
        {
            "$match": match
        },
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
                                    {"$gte": ["$date", a.isoformat()]},
                                    {"$lt": ["$date", b.isoformat()]}
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "dow": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date"
                                    }
                                }
                            }
                        }
                    }
                ],
                "as": "a"
            }
        },
        {
            "$unwind": {
                "path": "$a",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$department",

                "codes": {
                    "$addToSet": "$emp_code"
                },

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$a.status", PRESENCE]},
                                    {"$in": ["$a.dow", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$a.half_day", True]},
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
                                    {"$in": ["$a.status", PRESENCE]},
                                    {
                                        "$ne": [
                                            {"$ifNull": ["$a.work_hours", None]},
                                            None
                                        ]
                                    }
                                ]
                            },
                            "$a.work_hours",
                            None
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$a.late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$a.late_minutes", 0]}
                },

                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$a.status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },

                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$a.status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$codes"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {
            "$match": {
                "headcount": {"$gt": 0}
            }
        },
        {
            "$sort": {
                "department": 1
            }
        }
    ]

    rows = list(employees.aggregate(pipe))

    for item in rows:
        item["present_days"] = rnd(item.get("present_days", 0))
        item["avg_work_hours"] = rnd(item.get("avg_work_hours"))

    return {
        "month": month,
        "departments": rows
    }

@app.get('/analytics/leaderboard/late')
def leaderboard(month: str, limit: int=Query(10, ge=1, le=100), department: Optional[str]=None):
    a, b = parse_month(month)
    pipe = [{'$match': {'date': {'$gte': a.isoformat(), '$lt': b.isoformat()}, 'late_minutes': {'$gt': 0}}}, {'$group': {'_id': '$emp_code', 'total_late_minutes': {'$sum': '$late_minutes'}, 'late_days': {'$sum': 1}}}, {'$lookup': {'from': 'employees', 'localField': '_id', 'foreignField': 'emp_code', 'as': 'e'}}, {'$unwind': '$e'}]
    if department is not None:
        pipe.append({'$match': {'e.department': department}})
    pipe += [{'$setWindowFields': {'sortBy': {'total_late_minutes': -1}, 'output': {'rank': {'$rank': {}}}}}, {'$sort': {'total_late_minutes': -1, '_id': 1}}, {'$match': {'rank': {'$lte': limit}}}, {'$project': {'_id': 0, 'rank': 1, 'emp_code': '$_id', 'name': '$e.name', 'department': '$e.department', 'total_late_minutes': 1, 'late_days': 1}}]
    return {'month': month, 'leaderboard': list(logs.aggregate(pipe))}


@app.get("/analytics/departments/{department}/trend")
def trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    a = parse_date(from_date)
    b = parse_date(to_date)

    if b < a or (b - a).days > 91:
        raise HTTPException(
            422,
            "Invalid date range: 'to' must be >= 'from' and range <= 92 days"
        )

    if not employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    start = datetime(
        a.year, a.month, a.day, tzinfo=timezone.utc
    )
    stop = datetime(
        b.year, b.month, b.day, tzinfo=timezone.utc
    )
    end_exclusive = stop + timedelta(days=1)

    pipeline = [
        {
            "$documents": [
                {"_id": start}
            ]
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start, end_exclusive]
                }
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$_id",
                        "format": "%Y-%m-%d"
                    }
                },
                "dow": {"$dayOfWeek": "$_id"}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$day"]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee"
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "employee.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", PRESENCE]},
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
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "daily"
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.present_count", 0]},
                        0
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.late_count", 0]},
                        0
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                
    {
    "$lte": [
        "$joined_on",
        "$$day"
    ]
}
                                ]
                            }
                        }
                    },
                    {"$count": "n"}
                ],
                "as": "hc"
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$hc.n", 0]},
                        0
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount"
                                    ]
                                },
                                4
                            ]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": {
                    "$in": ["$dow", [2, 3, 4, 5, 6]]
                },
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        },
        {"$sort": {"date": 1}}
    ]

    rows = list(db.aggregate(pipeline))

    for item in rows:
        for key in ("attendance_rate", "moving_avg_7d"):
            if item.get(key) is not None:
                item[key] = rnd(item[key], 4)

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "trend": rows
    }


@app.get('/admin/explain/{endpoint}')
def explain(
    endpoint: Literal[
        'attendance_list',
        'employee_monthly',
        'department_summary',
        'late_leaderboard',
        'department_trend'
    ],
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias='from'),
    to_date: Optional[str] = Query(None, alias='to')
):
    if endpoint == 'attendance_list':
        q = {}

        if emp_code:
            q['emp_code'] = emp_code
        if status:
            q['status'] = status
        if date_from or date_to:
            q['date'] = {}
            if date_from:
                q['date']['$gte'] = date_from
            if date_to:
                q['date']['$lte'] = date_to

        exp = db.command(
            'explain',
            {
                'find': 'attendance_logs',
                'filter': q,
                'sort': {'date': -1, 'emp_code': 1},
                'skip': (page - 1) * page_size,
                'limit': page_size
            },
            verbosity='executionStats'
        )
        return {
            'endpoint': endpoint,
            'collection': 'attendance_logs',
            'explain': json.loads(json_util.dumps(exp))
        }

    if endpoint in (
        'employee_monthly',
        'department_summary',
        'late_leaderboard'
    ) and not month:
        raise HTTPException(422, 'month is required')

    if endpoint == 'employee_monthly' and not emp_code:
        raise HTTPException(422, 'emp_code is required')

    if endpoint == 'department_trend' and (
        not department or not from_date or not to_date
    ):
        raise HTTPException(422, 'department, from and to are required')

    if month:
        a, b = parse_month(month)

    if endpoint == 'employee_monthly':
        coll = 'attendance_logs'
        pipe = [
            {
                '$match': {
                    'emp_code': emp_code,
                    'date': {
                        '$gte': a.isoformat(),
                        '$lt': b.isoformat()
                    }
                }
            },
            {
                '$group': {
                    '_id': '$emp_code',
                    'n': {'$sum': 1}
                }
            }
        ]

    elif endpoint == 'department_summary':
        coll = 'employees'
        pipe = [
            {
                '$match': {
                    'joined_on': {'$lt': b.isoformat()},
                    **({'department': department} if department else {})
                }
            },
            {
                '$lookup': {
                    'from': 'attendance_logs',
                    'localField': 'emp_code',
                    'foreignField': 'emp_code',
                    'as': 'a'
                }
            },
            {
                '$unwind': {
                    'path': '$a',
                    'preserveNullAndEmptyArrays': True
                }
            },
            {
                '$group': {
                    '_id': '$department',
                    'n': {'$sum': 1}
                }
            }
        ]

    elif endpoint == 'late_leaderboard':
        coll = 'attendance_logs'
        pipe = [
            {
                '$match': {
                    'date': {
                        '$gte': a.isoformat(),
                        '$lt': b.isoformat()
                    },
                    'late_minutes': {'$gt': 0}
                }
            },
            {
                '$group': {
                    '_id': '$emp_code',
                    'total_late_minutes': {'$sum': '$late_minutes'}
                }
            },
            {'$sort': {'total_late_minutes': -1}},
            {'$limit': limit}
        ]

    else:
        parse_date(from_date)
        parse_date(to_date)
        coll = 'attendance_logs'
        pipe = [
            {
                '$match': {
                    'date': {
                        '$gte': from_date,
                        '$lte': to_date
                    }
                }
            },
            {
                '$group': {
                    '_id': '$date',
                    'n': {'$sum': 1}
                }
            }
        ]

    exp = db.command(
        'explain',
        {
            'aggregate': coll,
            'pipeline': pipe,
            'cursor': {}
        },
        verbosity='executionStats'
    )

    return {
        'endpoint': endpoint,
        'collection': coll,
        'explain': json.loads(json_util.dumps(exp))
    }
