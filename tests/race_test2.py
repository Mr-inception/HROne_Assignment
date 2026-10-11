import json, random, threading, urllib.request, urllib.error
from collections import Counter
from datetime import datetime, timedelta, timezone

BASE = "http://localhost:8000"
IST = timezone(timedelta(hours=5, minutes=30))

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")

def ms(dt):
    return int(dt.timestamp()) * 1000

def fire(n, fn):
    out = []
    threads = [threading.Thread(target=lambda i=i: out.append(fn(i))) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return out

day = datetime(2031, 1, 1, 9, 30, tzinfo=IST) + timedelta(days=random.randint(0, 2000))
d = day.date().isoformat()
print(call("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": ms(day)})[0], "punch-in (want 201)")

out = fire(10, lambda i: call("POST", "/attendance/punch-out",
                              {"emp_code": "EMP0001", "punched_at": ms(day + timedelta(hours=9))})[0])
print("punch-out race:", Counter(out), "(want one 200, nine 409)")

res = fire(8, lambda i: call("PATCH", f"/attendance/EMP0001/{d}",
                             {"punch_in": ms(day - timedelta(minutes=i + 1)),
                              "reason": "race test", "regularized_by": "tester"})[0])
print("patch race:", Counter(res))
_, data = call("GET", f"/attendance?emp_code=EMP0001&date_from={d}&date_to={d}")
print("history entries:", len(data["items"][0]["history"]), "| successful patches:", res.count(200), "(must be equal)")
