# DECISIONS.md

1. **Indexes.** employees: unique `emp_code` (identity and lookups), `(department, emp_code)` for the list, `joined_on` and `(department, joined_on)` for headcount. attendance_logs: unique `(emp_code, date)` (one record a day, race guard), `(date desc, emp_code)` for the default sort and month scans, `(status, date desc, emp_code)` for the status filter, `(emp_code, punch_in desc)` for punch-out. I rejected a separate `emp_code` index: the prefix of the unique one already covers it.

2. **Punch-in race.** Both requests read the employee and compute the same IST date, then insert. The unique `(emp_code, date)` index lets exactly one insert win. The other gets `DuplicateKeyError` and returns 409. There is no read-then-write gap to race through.

3. **Ties.** `$rank` gives standard competition ranks before `limit` is applied, so everyone tied at a rank up to `limit` is returned, even if that is more than `limit` rows. Order is minutes descending, then `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s the logs. Someone with no logs gets an empty result but still adds 1 to the headcount.

5. **100x data.** The trend sends every department `emp_code` in an `$in`. I would store the department on each log and keep daily pre-aggregated counts, and use keyset pagination instead of `skip`.
