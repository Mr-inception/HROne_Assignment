"""Compares /analytics endpoints with an independent pure-Python calculation.
Inserts clearly-named test data (EMP9xxxx, departments ZZ_*) and removes it afterwards."""
import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000)[
    os.getenv("MONGO_DB", "attendance_db")
]
BASE = "http://localhost:8000"
IST = timezone(timedelta(hours=5, minutes=30))
MONTH, FIRST, LAST = "2026-09", date(2026, 9, 1), date(2026, 9, 30)
D1, D2 = "ZZ_TEST", "ZZ_TEST2"
CODES = ["EMP90001", "EMP90002", "EMP90003", "EMP90004", "EMP90005", "EMP90099"]
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def at(day, hh, mm):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(timezone.utc)


def emp(code, dept, joined):
    return {"emp_code": code, "name": "Test " + code, "email": code.lower() + "@example.com",
            "department": dept, "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": joined, "created_at": datetime.now(timezone.utc)}


def log(code, day, status, tin=None, tout=None, wh=None, late=0, ot=0, half=False, legacy=False):
    d = date.fromisoformat(day)
    doc = {"emp_code": code, "date": day, "status": status,
           "punch_in": at(d, *tin) if tin else None, "punch_out": at(d, *tout) if tout else None,
           "work_hours": wh, "late_minutes": late, "overtime_minutes": ot,
           "half_day": half, "history": []}
    if legacy:  # old records: no history / half_day / late / overtime fields at all
        for k in ("history", "half_day", "late_minutes", "overtime_minutes"):
            doc.pop(k)
    return doc


EMPS = [
    emp("EMP90001", D1, "2026-01-01"),
    emp("EMP90002", D1, "2026-09-16"),   # joined mid-month
    emp("EMP90003", D1, "2026-01-01"),   # no logs at all
    emp("EMP90004", D1, "2026-10-05"),   # joins after the month
    emp("EMP90005", D2, "2026-01-01"),   # 8.12 and 8.13 -> average 8.125
]
LOGS = [
    log("EMP90001", "2026-08-31", "PRESENT", (9, 30), (18, 30), 9.0),          # outside month
    log("EMP90001", "2026-09-01", "PRESENT", (9, 30), (18, 30), 9.0),
    log("EMP90001", "2026-09-02", "PRESENT", (10, 15), (19, 10), 8.92, late=45, ot=40),
    log("EMP90001", "2026-09-03", "WFH", (9, 30), (13, 0), 3.5, half=True),
    log("EMP90001", "2026-09-04", "ON_DUTY", (9, 30), (18, 45), 9.25),
    log("EMP90001", "2026-09-05", "PRESENT", (11, 0), (20, 0), 9.0, late=90, ot=90),   # Saturday
    log("EMP90001", "2026-09-07", "LEAVE"),
    log("EMP90001", "2026-09-08", "ABSENT"),
    log("EMP90001", "2026-09-09", "PRESENT", (9, 42), late=12),                       # still open
    log("EMP90001", "2026-09-10", "PRESENT", (9, 30), (14, 0), 4.5),
    log("EMP90001", "2026-09-11", "PRESENT", (9, 30), (13, 59), 4.49, half=True),
    log("EMP90001", "2026-09-13", "WFH", (10, 0), (16, 0), 6.0),                      # Sunday
    log("EMP90001", "2026-09-14", "PRESENT", (9, 30), (18, 28), 8.98, legacy=True),
    log("EMP90001", "2026-10-01", "PRESENT", (9, 30), (18, 30), 9.0),                 # outside month
    log("EMP90002", "2026-09-16", "PRESENT", (9, 30), (18, 30), 9.0),
    log("EMP90002", "2026-09-17", "PRESENT", (9, 30), (13, 30), 4.0, half=True),
    log("EMP90002", "2026-09-18", "LEAVE"),
    log("EMP90002", "2026-09-19", "PRESENT", (9, 45), (18, 30), 8.75, late=15),       # Saturday
    log("EMP90005", "2026-09-01", "PRESENT", (9, 30), (17, 38), 8.12),
    log("EMP90005", "2026-09-02", "PRESENT", (9, 30), (17, 39), 8.13),
    log("EMP90099", "2026-09-01", "PRESENT", (9, 30), (18, 30), 9.0),                 # orphan log
]


# ----------------------------- independent reference ------------------------------ #
def weekdays(a, b):
    n, d = 0, a
    while d <= b:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def q2(x):
    return float(Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def sums(docs):
    s = dict(present=Decimal(0), leave=0, on_duty=0, late_count=0, late=0, ot=0, cents=0, cnt=0)
    for l in docs:
        day = date.fromisoformat(l["date"])
        if not (FIRST <= day <= LAST):
            continue
        late = l.get("late_minutes") or 0
        s["late"] += late
        s["late_count"] += late > 0
        s["ot"] += l.get("overtime_minutes") or 0
        s["leave"] += l["status"] == "LEAVE"
        s["on_duty"] += l["status"] == "ON_DUTY"
        if l["status"] in PRESENT:
            if day.weekday() < 5:
                s["present"] += Decimal("0.5") if l.get("half_day") else Decimal(1)
            if l.get("work_hours") is not None:
                s["cents"] += int((Decimal(str(l["work_hours"])) * 100).to_integral_value(ROUND_HALF_UP))
                s["cnt"] += 1
    return s


def expected_monthly(e):
    joined = date.fromisoformat(e["joined_on"])
    start = max(FIRST, joined)
    wd = weekdays(start, LAST) if start <= LAST else 0
    s = sums([l for l in LOGS if l["emp_code"] == e["emp_code"]])
    return {"emp_code": e["emp_code"], "month": MONTH, "working_days": wd,
            "present_days": q2(s["present"]), "leave_days": s["leave"], "late_count": s["late_count"],
            "total_late_minutes": s["late"], "total_overtime_minutes": s["ot"],
            "attendance_pct": q2(s["present"] * 100 / wd) if wd else None}


def expected_dept(dept):
    members = [e for e in EMPS if e["department"] == dept and date.fromisoformat(e["joined_on"]) <= LAST]
    if not members:
        return None
    codes = {e["emp_code"] for e in members}
    s = sums([l for l in LOGS if l["emp_code"] in codes])
    return {"department": dept, "headcount": len(members), "present_days": q2(s["present"]),
            "avg_work_hours": q2(Decimal(s["cents"]) / s["cnt"] / 100) if s["cnt"] else None,
            "late_count": s["late_count"], "total_late_minutes": s["late"],
            "leave_count": s["leave"], "on_duty_count": s["on_duty"]}


# ---------------------------------- test harness ----------------------------------- #
def get(path):
    try:
        with urllib.request.urlopen(BASE + path) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


failures = 0


def check(label, got, want):
    global failures
    ok = got == want
    failures += not ok
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok:
        print("   got :", got)
        print("   want:", want)


def cleanup():
    db.employees.delete_many({"emp_code": {"$in": CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": CODES}})


def main():
    cleanup()
    try:
        db.employees.insert_many([dict(e) for e in EMPS])
        db.attendance_logs.insert_many([dict(l) for l in LOGS])

        for e in EMPS:
            st, body = get(f"/analytics/employees/{e['emp_code']}/monthly?month={MONTH}")
            check(f"monthly {e['emp_code']}", (st, body), (200, expected_monthly(e)))
        check("monthly unknown employee -> 404", get(f"/analytics/employees/EMP99999/monthly?month={MONTH}")[0], 404)
        check("monthly bad month -> 422", get("/analytics/employees/EMP90001/monthly?month=2026-13")[0], 422)
        check("monthly missing month -> 422", get("/analytics/employees/EMP90001/monthly")[0], 422)

        for dept in (D1, D2):
            st, body = get(f"/analytics/departments/summary?month={MONTH}&department={dept}")
            check(f"summary {dept}", (st, body), (200, {"month": MONTH, "items": [expected_dept(dept)]}))
        check("summary unknown department -> empty",
              get(f"/analytics/departments/summary?month={MONTH}&department=ZZ_NONE"),
              (200, {"month": MONTH, "items": []}))
        st, body = get(f"/analytics/departments/summary?month={MONTH}")
        names = [i["department"] for i in body["items"]]
        check("summary (all) sorted by department", names, sorted(names))
        mine = {i["department"]: i for i in body["items"]}
        check("summary (all) contains ZZ_TEST unchanged", mine.get(D1), expected_dept(D1))
        check("summary (all) contains ZZ_TEST2 unchanged", mine.get(D2), expected_dept(D2))
        check("summary bad month -> 422", get("/analytics/departments/summary?month=2026-00")[0], 422)
    finally:
        cleanup()
    print("\nALL PASSED" if not failures else f"\n{failures} CHECK(S) FAILED")


if __name__ == "__main__":
    main()

