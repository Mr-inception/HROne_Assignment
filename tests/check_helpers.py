import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from datetime import datetime, timezone
from app.main import *

def ist(h, m, s=0, day=6):
    return datetime(2026, 7, day, h, m, s, tzinfo=IST).astimezone(timezone.utc)

print("late 09:40:00 / 09:40:01 / 10:15:59 ->",
      [compute_late_minutes(ist(*t), "2026-07-06", "09:30", "18:30") for t in [(9,40,0),(9,40,1),(10,15,59)]], "(want [0, 10, 45])")
print("overnight date 03:00 / 06:00 ->",
      attendance_date(ist(3,0,0,7), "22:00", "06:00"), attendance_date(ist(6,0,0,7), "22:00", "06:00"),
      "(want 2026-07-06 2026-07-07)")
print("overtime 19:10 / 18:55 ->",
      compute_overtime_minutes(ist(19,10), "2026-07-06", "09:30", "18:30"),
      compute_overtime_minutes(ist(18,55), "2026-07-06", "09:30", "18:30"), "(want 40 0)")
print("overnight overtime 06:40 next day ->",
      compute_overtime_minutes(ist(6,40,0,7), "2026-07-06", "22:00", "06:00"), "(want 40)")
print("work hours 09:28-18:35 ->", compute_work_hours(ist(9,28), ist(18,35)), "(want 9.12)")
print("work hours 2h40m30s ->", compute_work_hours(ist(9,0), ist(11,40,30)), "(want 2.68, half-up)")
print("half day 4.49 / 4.5 ->", compute_half_day(4.49), compute_half_day(4.5), "(want True False)")

