"""Checks leaderboard, trend and explain against an independent pure-Python calculation.
Inserts clearly-named test data (EMP91xxx / EMP92xxx, departments ZZ_*) and removes it afterwards."""
import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True,
                 serverSelectionTimeoutMS=5000)[os.getenv("MONGO_DB", "attendance_db")]
BASE = "http://localhost:8000"
IST = timezone(timedelta(hours=5, minutes=30))
MONTH, FIRST, LAST = "2027-03", date(2027, 3, 1), date(2027, 3, 31)
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def at(day, hh, mm):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(timezone.utc)


def emp(code, dept, joined):
    return {"emp_code": code, "name": "Name " + code[-3:], "email": code.lower() + "@example.com",
            "department": dept, "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": joined, "created_at": datetime.now(timezone.utc)}


def log(code, day, status="PRESENT", late=0, half=False, work=True):
    d = date.fromisoformat(day)
    has = status in PRESENT
    return {"emp_code": code, "date": day, "status": status,
            "punch_in": at(d, 9, 30) if has else None,
            "punch_out": at(d, 18, 30) if has and work else None,
            "work_hours": 9.0 if has and work else None, "late_minutes": late,
            "overtime_minutes": 0, "half_day": half, "history": []}


LB, LB2, TR, TR2 = "ZZ_LB", "ZZ_LB2", "ZZ_TR", "ZZ_TR2"
EMPS = [
    emp("EMP91001", LB, "2027-01-01"), emp("EMP91002", LB, "2027-01-01"), emp("EMP91003", LB, "2027-01-01"),
    emp("EMP91004", LB, "2027-01-01"), emp("EMP91005", LB, "2027-01-01"), emp("EMP91006", LB, "2027-01-01"),
    emp("EMP91007", LB2, "2027-01-01"),
    emp("EMP92001", TR, "2027-01-01"), emp("EMP92002", TR, "2027-03-10"),   # joins inside the range
    emp("EMP92003", TR, "2027-03-20"), emp("EMP92004", TR, "2027-01-01"),   # 92004 never has logs
    emp("EMP92005", TR2, "2027-01-01"),                                      # other department
]
LOGS = [
    # --- leaderboard: 100 / 60 / 60 / 60 / 60(other dept) / 10 / none / orphan(ignored) ---
    log("EMP91001", "2027-03-01", late=70), log("EMP91001", "2027-03-02", late=30),
    log("EMP91002", "2027-03-01", late=60),
    log("EMP91003", "2027-03-02", late=40), log("EMP91003", "2027-03-06", late=20),     # Saturday counts
    log("EMP91004", "2027-03-03", late=60),
    log("EMP91005", "2027-03-04", late=10), log("EMP91005", "2027-03-05", late=0),
    log("EMP91005", "2027-02-28", late=300), log("EMP91005", "2027-04-01", late=300),   # outside month
    log("EMP91006", "2027-03-01", late=0),
    log("EMP91007", "2027-03-01", late=60),
    log("EMP91099", "2027-03-01", late=500),                                            # no such employee
    # --- trend (range 2027-03-04 .. 2027-03-26) ---
    log("EMP92001", "2027-03-03"), log("EMP92001", "2027-03-27"),                       # outside range
    log("EMP92001", "2027-03-04"), log("EMP92001", "2027-03-05", late=20),
    log("EMP92001", "2027-03-06", late=15),                                             # Saturday
    log("EMP92001", "2027-03-09"), log("EMP92001", "2027-03-10", "WFH", half=True),
    log("EMP92001", "2027-03-11", "LEAVE"), log("EMP92001", "2027-03-12", "ABSENT"),
    log("EMP92001", "2027-03-15"), log("EMP92001", "2027-03-16", late=12),
    log("EMP92001", "2027-03-17", "ON_DUTY"), log("EMP92001", "2027-03-18"), log("EMP92001", "2027-03-19"),
    log("EMP92001", "2027-03-22"), log("EMP92001", "2027-03-23"), log("EMP92001", "2027-03-24"),
    log("EMP92002", "2027-03-10"), log("EMP92002", "2027-03-11", half=True), log("EMP92002", "2027-03-12"),
    log("EMP92002", "2027-03-15"), log("EMP92002", "2027-03-16"), log("EMP92002", "2027-03-21", "WFH"),
    log("EMP92003", "2027-03-22"), log("EMP92003", "2027-03-23", late=30), log("EMP92003", "2027-03-24"),
    log("EMP92005", "2027-03-04"), log("EMP92005", "2027-03-05", late=99), log("EMP92005", "2027-03-09"),
]
CODES = sorted({e["emp_code"] for e in EMPS} | {l["emp_code"] for l in LOGS})


# ----------------------------- independent reference ------------------------------ #
def r4(x):
    return Decimal(x).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def expected_leaderboard(limit, department=None):
    dept_of = {e["emp_code"]: e["department"] for e in EMPS}
    tot, cnt = {}, {}
    for l in LOGS:
        if l["emp_code"] not in dept_of or not (FIRST <= date.fromisoformat(l["date"]) <= LAST):
            continue
        tot[l["emp_code"]] = tot.get(l["emp_code"], 0) + l["late_minutes"]
        cnt[l["emp_code"]] = cnt.get(l["emp_code"], 0) + (l["late_minutes"] > 0)
    rows = [(c, t) for c, t in tot.items() if t > 0 and (department is None or dept_of[c] == department)]
    rows.sort(key=lambda r: (-r[1], r[0]))
    out = []
    for i, (c, t) in enumerate(rows):
        rank = 1 + sum(1 for _, t2 in rows if t2 > t)          # 1 + number of strictly larger totals
        if rank <= limit:
            e = next(x for x in EMPS if x["emp_code"] == c)
            out.append({"rank": rank, "emp_code": c, "name": e["name"], "department": e["department"],
                        "total_late_minutes": t, "late_count": cnt[c]})
    return {"month": MONTH, "items": out}


def expected_trend(dept, d_from, d_to):
    members = [e for e in EMPS if e["department"] == dept]
    codes = {e["emp_code"] for e in members}
    rows, rates = [], []
    day = d_from
    while day <= d_to:
        ds = day.isoformat()
        hc = sum(1 for e in members if e["joined_on"] <= ds)
        todays = [l for l in LOGS if l["emp_code"] in codes and l["date"] == ds]
        present = sum((Decimal("0.5") if l["half_day"] else Decimal(1)) for l in todays if l["status"] in PRESENT)
        working = day.weekday() < 5
        rate = r4(present / hc) if working and hc > 0 else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        moving = r4(sum(window) / len(window)) if window else None
        rows.append({"date": ds, "is_working_day": working, "headcount": hc, "present_count": float(present),
                     "late_count": sum(1 for l in todays if l["late_minutes"] > 0),
                     "attendance_rate": None if rate is None else float(rate),
                     "moving_avg_7d": None if moving is None else float(moving)})
        day += timedelta(days=1)
    return {"department": dept, "items": rows}


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
        print("   got :", json.dumps(got)[:1500])
        print("   want:", json.dumps(want)[:1500])


def stages(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "stage" and isinstance(v, str):
                out.append(v)
            stages(v, out)
    elif isinstance(obj, list):
        for v in obj:
            stages(v, out)
    return out


def check_explain(label, query):
    st, body = get("/admin/explain/" + query)
    text = json.dumps(body)
    found = sorted(set(stages(body.get("explain", {})))) if st == 200 else []
    ok = st == 200 and "IXSCAN" in text and "COLLSCAN" not in text
    global failures
    failures += not ok
    print(("PASS  " if ok else "FAIL  ") + f"explain {label}  [{', '.join(found)}]" + ("" if st == 200 else f"  HTTP {st}"))


def cleanup():
    db.employees.delete_many({"emp_code": {"$in": CODES}})
    db.attendance_logs.delete_many({"emp_code": {"$in": CODES}})


def main():
    cleanup()
    try:
        db.employees.insert_many([dict(e) for e in EMPS])
        db.attendance_logs.insert_many([dict(l) for l in LOGS])

        # ---- leaderboard
        for limit in (1, 2, 3, 5, 10):
            check(f"leaderboard limit={limit}", get(f"/analytics/leaderboard/late?month={MONTH}&limit={limit}"),
                  (200, expected_leaderboard(limit)))
        for dept in (LB, LB2):
            check(f"leaderboard department={dept} limit=2",
                  get(f"/analytics/leaderboard/late?month={MONTH}&limit=2&department={dept}"),
                  (200, expected_leaderboard(2, dept)))
        check("leaderboard empty month", get("/analytics/leaderboard/late?month=2027-05"),
              (200, {"month": "2027-05", "items": []}))
        check("leaderboard limit=51 -> 422", get(f"/analytics/leaderboard/late?month={MONTH}&limit=51")[0], 422)
        check("leaderboard limit=0 -> 422", get(f"/analytics/leaderboard/late?month={MONTH}&limit=0")[0], 422)
        check("leaderboard bad month -> 422", get("/analytics/leaderboard/late?month=2027-13")[0], 422)

        # ---- trend
        d_from, d_to = date(2027, 3, 4), date(2027, 3, 26)
        check("trend ZZ_TR 2027-03-04..26", get(f"/analytics/departments/{TR}/trend?from={d_from}&to={d_to}"),
              (200, expected_trend(TR, d_from, d_to)))
        check("trend ZZ_TR2 (other department isolated)", get(f"/analytics/departments/{TR2}/trend?from={d_from}&to={d_to}"),
              (200, expected_trend(TR2, d_from, d_to)))
        check("trend single day", get(f"/analytics/departments/{TR}/trend?from=2027-03-08&to=2027-03-08"),
              (200, expected_trend(TR, date(2027, 3, 8), date(2027, 3, 8))))
        st, body = get(f"/analytics/departments/{TR}/trend?from=2027-01-01&to=2027-04-02")
        check("trend 92 days -> 200 with 92 rows", (st, len(body["items"])), (200, 92))
        check("trend 93 days -> 422", get(f"/analytics/departments/{TR}/trend?from=2027-01-01&to=2027-04-03")[0], 422)
        check("trend to < from -> 422", get(f"/analytics/departments/{TR}/trend?from=2027-03-10&to=2027-03-09")[0], 422)
        check("trend bad date -> 422", get(f"/analytics/departments/{TR}/trend?from=2027-02-30&to=2027-03-09")[0], 422)
        check("trend missing param -> 422", get(f"/analytics/departments/{TR}/trend?from=2027-03-01")[0], 422)
        check("trend unknown department -> 404", get("/analytics/departments/ZZ_NOPE/trend?from=2027-03-01&to=2027-03-05")[0], 404)

        # ---- explain: IXSCAN present, COLLSCAN absent
        check_explain("attendance_list (no filter)", "attendance_list")
        check_explain("attendance_list (emp_code)", "attendance_list?emp_code=EMP91001")
        check_explain("attendance_list (date range)", "attendance_list?date_from=2027-03-01&date_to=2027-03-31&page=2&page_size=5")
        check_explain("attendance_list (status)", "attendance_list?status=LEAVE")
        check_explain("employee_monthly", f"employee_monthly?emp_code=EMP91001&month={MONTH}")
        check_explain("department_summary", f"department_summary?month={MONTH}")
        check_explain("department_summary (dept)", f"department_summary?month={MONTH}&department={LB}")
        check_explain("late_leaderboard", f"late_leaderboard?month={MONTH}&limit=5")
        check_explain("late_leaderboard (dept)", f"late_leaderboard?month={MONTH}&department={LB}")
        check_explain("department_trend", f"department_trend?department={TR}&from=2027-03-04&to=2027-03-26")
        check("explain employee_monthly missing month -> 422", get("/admin/explain/employee_monthly?emp_code=EMP91001")[0], 422)
        check("explain department_trend missing from -> 422", get(f"/admin/explain/department_trend?department={TR}&to=2027-03-26")[0], 422)
        check("explain unknown endpoint -> 422", get("/admin/explain/nope")[0], 422)
    finally:
        cleanup()
    print("\nALL PASSED" if not failures else f"\n{failures} CHECK(S) FAILED")


if __name__ == "__main__":
    main()
