import json, random, threading, urllib.request, urllib.error
from collections import Counter
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
day = datetime(2030, 1, 1, 9, 30, tzinfo=IST) + timedelta(days=random.randint(0, 3000))
body = json.dumps({"emp_code": "EMP0001", "punched_at": int(day.timestamp()) * 1000}).encode()
results = []

def go():
    req = urllib.request.Request("http://localhost:8000/attendance/punch-in", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        results.append(urllib.request.urlopen(req).status)
    except urllib.error.HTTPError as e:
        results.append(e.code)

threads = [threading.Thread(target=go) for _ in range(10)]
[t.start() for t in threads]
[t.join() for t in threads]
print(Counter(results), "(want one 201 and nine 409)")
