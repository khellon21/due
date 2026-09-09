# Assignment Due Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push escalating notifications to Khellon's phone about Blackboard assignment deadlines, avoiding his work shifts, until he marks each assignment done.

**Architecture:** No long-running scheduler. A cron job runs `due.py tick` every five minutes; each run refreshes a cached .ics, decides which single notification stage each assignment is currently in, sends what has not been sent, and exits. A small Flask page handles "mark done" and work-schedule edits. State lives in SQLite.

**Tech Stack:** Python 3.12 (Ubuntu 24.04 default), `icalendar`, `flask`, `waitress`. Everything else is stdlib — `sqlite3`, `urllib.request`, `zoneinfo`, `json`, `hmac`.

**Spec:** `docs/superpowers/specs/2026-09-08-assignment-due-notifier-design.md`

## Global Constraints

- Target host: Oracle Cloud Always Free E2.1.Micro, 1 OCPU, 1 GB RAM, Ubuntu 24.04. Keep resident memory minimal; the tick must be a short-lived process, not a daemon.
- Timezone is `America/New_York` throughout. Every datetime that is stored or compared is timezone-aware. Never use `datetime.now()` without a tz.
- All datetimes are persisted as ISO 8601 strings with offset, via `.isoformat()` / `datetime.fromisoformat()`.
- Every tunable is a `config.json` key, never a literal in the code: `heads_up_hour`, `quiet_hours`, `repeat_minutes`, `escalation_hours`.
- `config.json`, `due.db`, `learn.ics` and `tick.log` are runtime state and must be gitignored. Only `config.example.json` is committed.
- Testing is assertion-based in a single `test_due.py`, run as `python test_due.py`. No pytest, no fixtures, no test framework.
- Pure logic functions (`is_work_day`, `heads_up_at`, `current_stage`, `in_quiet_hours`, `notification`) take their inputs as arguments and read no global state, so they are directly testable against a fixed clock.
- Day-of-week keys are exactly `Mon Tue Wed Thu Fri Sat Sun`.

## File Structure

All files live at the repository root, deployed to `~/due/` on the VM.

| File | Responsibility |
|---|---|
| `due.py` | Config, feed fetch/parse, scheduling rules, ntfy sending, SQLite, `tick` entry point |
| `web.py` | Flask page: mark done, edit work schedule. Imports `due.py` |
| `config.example.json` | Committed template; copied to `config.json` on the VM |
| `test_due.py` | Assertion self-check for everything in `due.py` |
| `README.md` | VM setup: venv, cron, systemd, both firewall layers, logrotate |
| `.gitignore` | Runtime state |

`due.py` stays a single module (~250 lines). Splitting it into store/ics/schedule/notify modules would produce four files of fifty lines each plus import ceremony, for no gain in testability — the functions are already pure and independently callable.

---

### Task 1: Skeleton, config, and the work-day rule

**Files:**
- Create: `.gitignore`
- Create: `config.example.json`
- Create: `due.py`
- Create: `test_due.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `due.DAY_NAMES: list[str]` — `["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]`, indexed by `date.weekday()`
  - `due.HERE: pathlib.Path` — directory containing `due.py`
  - `due.load_config() -> dict`
  - `due.save_config(cfg: dict) -> None` — atomic replace
  - `due.is_work_day(day: datetime.date, work_schedule: dict) -> bool`

- [ ] **Step 1: Write the failing test**

Create `test_due.py`:

```python
"""Self-check for due.py.  Run: python test_due.py"""
import sys
from datetime import date

import due


def test_is_work_day():
    sched = {"Mon": ["16:00-21:00"], "Tue": [], "Wed": []}
    assert due.is_work_day(date(2026, 9, 7), sched) is True, "Monday has a shift"
    assert due.is_work_day(date(2026, 9, 8), sched) is False, "Tuesday is listed but empty"
    assert due.is_work_day(date(2026, 9, 10), sched) is False, "Thursday is absent entirely"


def test_config_roundtrip():
    cfg = due.load_config(due.HERE / "config.example.json")
    assert cfg["timezone"] == "America/New_York"
    assert set(cfg["work_schedule"]) == set(due.DAY_NAMES)
    assert cfg["escalation_hours"] == [5, 2, 1]


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_due.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'due'`

- [ ] **Step 3: Write minimal implementation**

Create `.gitignore`:

```
config.json
due.db
learn.ics
tick.log
.venv/
__pycache__/
```

Create `config.example.json`:

```json
{
  "ics_url": "https://edisonohio.blackboard.com/webapps/calendar/calendarFeed/3815adaf6ea845268b8dcc627c422d88/learn.ics",
  "timezone": "America/New_York",
  "ntfy_server": "https://ntfy.sh",
  "ntfy_topic": "CHANGE-ME-to-a-long-random-string",
  "base_url": "http://CHANGE-ME-vm-ip:8080",
  "web_token": "CHANGE-ME-32-hex-characters",
  "heads_up_hour": 20,
  "quiet_hours": [0, 8],
  "repeat_minutes": 30,
  "escalation_hours": [5, 2, 1],
  "work_schedule": {
    "Mon": [],
    "Tue": [],
    "Wed": [],
    "Thu": [],
    "Fri": [],
    "Sat": [],
    "Sun": []
  }
}
```

Create `due.py`:

```python
"""Blackboard assignment due-date notifier.

Run `python due.py tick` from cron every five minutes.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).parent
CONFIG_PATH = HERE / "config.json"

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def load_config(path=None):
    return json.loads((path or CONFIG_PATH).read_text())


def save_config(cfg, path=None):
    """Write atomically: the web page may save while a tick is reading."""
    path = path or CONFIG_PATH
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(path)


def is_work_day(day, work_schedule):
    """True if `day` has at least one shift. A shift blocks the whole day."""
    return bool(work_schedule.get(DAY_NAMES[day.weekday()]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_due.py`
Expected: `2/2 passed`

- [ ] **Step 5: Commit**

```bash
git add .gitignore config.example.json due.py test_due.py
git commit -m "feat: config loading and the work-day rule"
```

---

### Task 2: Fetch and parse the Blackboard feed

**Files:**
- Modify: `due.py`
- Modify: `test_due.py`

**Interfaces:**
- Consumes: `due.HERE`, `due.load_config`
- Produces:
  - `due.CACHE: pathlib.Path` — `HERE / "learn.ics"`
  - `due.fetch_ics(cfg: dict, max_age: int = 3600, now: float | None = None, cache: pathlib.Path | None = None) -> bytes`
  - `due.parse_ics(data: bytes, tz: ZoneInfo) -> list[tuple[str, str, datetime]]` — `(uid, title, due)`, `due` converted to `tz`

- [ ] **Step 1: Write the failing test**

Add to `test_due.py` (and extend the imports at the top to
`from datetime import date, datetime` plus `from zoneinfo import ZoneInfo`):

```python
TZ = ZoneInfo("America/New_York")

# Exercises the two things a hand-rolled parser gets wrong: RFC 5545 comma
# escaping, and a summary folded across two lines with a leading space.
FIXTURE = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:item-1
SUMMARY:Chapter 9 Quiz- Emotion\\, Stress\\, and Health
DTSTART;TZID=America/New_York:20260910T235900
END:VEVENT
BEGIN:VEVENT
UID:item-2
SUMMARY:Topic 05 - The Importance of Asset Documentation in System
 s Administration
DTSTART;TZID=America/New_York:20260928T235943
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_unescapes_and_unfolds():
    events = dict((uid, (title, dt)) for uid, title, dt in due.parse_ics(FIXTURE, TZ))
    assert events["item-1"][0] == "Chapter 9 Quiz- Emotion, Stress, and Health"
    assert events["item-2"][0] == (
        "Topic 05 - The Importance of Asset Documentation in Systems Administration"
    )


def test_parse_ics_returns_aware_local_datetimes():
    _, _, dt = due.parse_ics(FIXTURE, TZ)[0]
    assert dt == datetime(2026, 9, 10, 23, 59, tzinfo=TZ)
    assert dt.tzinfo is TZ, "due datetimes are normalised to the configured zone"


def test_fetch_ics_uses_cache_when_fresh():
    # A scratch cache path, so this never clobbers the real learn.ics -- a tick
    # run afterwards would sync against this 2-event fixture and deactivate
    # every real assignment.
    scratch = due.HERE / "test-cache.ics"
    scratch.write_bytes(FIXTURE)
    attempts = []

    def spy(*args, **kwargs):
        attempts.append(args)
        raise OSError("network unreachable")

    real_urlopen = due.urllib.request.urlopen
    due.urllib.request.urlopen = spy
    try:
        cfg = {"ics_url": "http://example.invalid/feed.ics"}
        assert due.fetch_ics(cfg, max_age=3600, cache=scratch) == FIXTURE
        assert attempts == [], "a fresh cache must not touch the network at all"
        # Aged out: the refresh must be attempted, and its failure must fall
        # back to the stale copy rather than silencing the notifier.
        assert due.fetch_ics(cfg, max_age=0, cache=scratch) == FIXTURE
        assert len(attempts) == 1, "a stale cache must attempt a refresh"
    finally:
        due.urllib.request.urlopen = real_urlopen
        scratch.unlink()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_due.py`
Expected: FAIL — `AttributeError: module 'due' has no attribute 'parse_ics'`

- [ ] **Step 3: Write minimal implementation**

Install the dependency first:

```bash
python -m venv .venv && .venv/bin/pip install icalendar flask waitress
```

Add to `due.py` (imports at the top, functions below `is_work_day`):

```python
import urllib.request
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from icalendar import Calendar

CACHE = HERE / "learn.ics"


def fetch_ics(cfg, max_age=3600, now=None, cache=None):
    """Return the feed bytes, re-downloading only if the cache has aged out.

    The feed advertises X-PUBLISHED-TTL:PT4H, so an hourly refresh is polite
    and still fresh. A network failure falls back to the cache: a blip must
    never silence the notifications.
    """
    cache = cache or CACHE
    now = now if now is not None else time.time()
    if cache.exists() and now - cache.stat().st_mtime < max_age:
        return cache.read_bytes()
    try:
        with urllib.request.urlopen(cfg["ics_url"], timeout=30) as r:
            data = r.read()
    except Exception:
        if cache.exists():
            return cache.read_bytes()
        raise
    cache.write_bytes(data)
    return data


def parse_ics(data, tz):
    """(uid, title, due) for every timed VEVENT, due normalised to `tz`."""
    out = []
    for ev in Calendar.from_ical(data).walk("VEVENT"):
        dt = ev.decoded("DTSTART")
        if not isinstance(dt, datetime):
            continue  # VALUE=DATE all-day entry, not a deadline
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        out.append((str(ev["UID"]), str(ev["SUMMARY"]), dt.astimezone(tz)))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python test_due.py`
Expected: `5/5 passed`

If `test_parse_ics_returns_aware_local_datetimes` fails on the `TZID` lookup,
the installed `icalendar` is too old to resolve zone names via `zoneinfo`.
Upgrade with `.venv/bin/pip install -U 'icalendar>=6'` rather than adding a
`VTIMEZONE` block to the fixture — the real feed relies on the same lookup.

- [ ] **Step 5: Commit**

```bash
git add due.py test_due.py
git commit -m "feat: fetch and parse the Blackboard ics feed"
```

---

### Task 3: The scheduling rules

This is the heart of the system. Every notification time comes from these three functions, and they are pure — no clock, no database, no config file.

**Files:**
- Modify: `due.py`
- Modify: `test_due.py`

**Interfaces:**
- Consumes: `due.is_work_day`, `due.DAY_NAMES`
- Produces:
  - `due.HEADS_UP: str = "HEADS_UP"`, `due.REPEAT: str = "REPEAT"`
  - `due.heads_up_at(due_dt, work_schedule, today, hour) -> datetime`
  - `due.current_stage(now, due_dt, heads_up, escalation_hours=(5,2,1), repeat_minutes=30) -> str | None` — returns `HEADS_UP`, `"T5H"`/`"T2H"`/`"T1H"` (named from `escalation_hours`), `REPEAT`, or `None`
  - `due.in_quiet_hours(hour: int, quiet: list[int]) -> bool`

- [ ] **Step 1: Write the failing test**

Add to `test_due.py` (extend the datetime import to include `timedelta`; the
tick tests below also need `io` and `contextlib`):

```python
DUE = datetime(2026, 9, 10, 23, 59, tzinfo=TZ)  # Thursday, a real feed item
FREE = {d: [] for d in due.DAY_NAMES}
WED_SHIFT = dict(FREE, Wed=["16:00-21:00"])
WED_AND_TUE_SHIFT = dict(FREE, Wed=["16:00-21:00"], Tue=["16:00-21:00"])


def test_heads_up_lands_the_evening_before_when_free():
    got = due.heads_up_at(DUE, FREE, date(2026, 9, 8), 20)
    assert got == datetime(2026, 9, 9, 20, 0, tzinfo=TZ), got


def test_heads_up_skips_a_whole_work_day():
    got = due.heads_up_at(DUE, WED_SHIFT, date(2026, 9, 8), 20)
    assert got == datetime(2026, 9, 8, 20, 0, tzinfo=TZ), got


def test_heads_up_falls_back_when_every_day_is_a_work_day():
    got = due.heads_up_at(DUE, WED_AND_TUE_SHIFT, date(2026, 9, 8), 20)
    assert got == datetime(2026, 9, 9, 20, 0, tzinfo=TZ), "never stay silent"


def test_stage_boundaries():
    hu = datetime(2026, 9, 9, 20, 0, tzinfo=TZ)
    cases = [
        (hu - timedelta(seconds=1), None),
        (hu, due.HEADS_UP),
        (DUE - timedelta(hours=5, seconds=1), due.HEADS_UP),
        (DUE - timedelta(hours=5), "T5H"),
        (DUE - timedelta(hours=2, seconds=1), "T5H"),
        (DUE - timedelta(hours=2), "T2H"),
        (DUE - timedelta(hours=1, seconds=1), "T2H"),
        (DUE - timedelta(hours=1), "T1H"),
        (DUE - timedelta(minutes=30, seconds=1), "T1H"),
        (DUE - timedelta(minutes=30), due.REPEAT),
        (DUE, due.REPEAT),
        (DUE + timedelta(days=3), due.REPEAT),
    ]
    for now, expected in cases:
        assert due.current_stage(now, DUE, hu) == expected, f"at {now} expected {expected}"


def test_heads_up_skipped_when_it_would_overlap_the_escalation():
    early = datetime(2026, 9, 11, 0, 30, tzinfo=TZ)   # due just after midnight
    hu = datetime(2026, 9, 10, 20, 0, tzinfo=TZ)      # 8pm is later than due-5h
    assert due.current_stage(datetime(2026, 9, 10, 20, 0, tzinfo=TZ), early, hu) == "T5H"


def test_quiet_hours():
    assert due.in_quiet_hours(2, [0, 8]) is True
    assert due.in_quiet_hours(8, [0, 8]) is False
    assert due.in_quiet_hours(23, [0, 8]) is False
    assert due.in_quiet_hours(23, [22, 6]) is True, "must handle a wrapping window"
    assert due.in_quiet_hours(3, [22, 6]) is True
    assert due.in_quiet_hours(12, [22, 6]) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python test_due.py`
Expected: FAIL — `AttributeError: module 'due' has no attribute 'heads_up_at'`

- [ ] **Step 3: Write minimal implementation**

Add to `due.py` (extend the datetime import to `from datetime import datetime, timedelta`).
Do NOT import `time` from `datetime`: `due.py` already imports the stdlib `time`
module for `fetch_ics`, and `from datetime import time` would shadow it, breaking
`time.time()` with `AttributeError: type object 'datetime.time' has no attribute
'time'`. The heads-up datetime is built directly instead, which needs no `time`:

```python
HEADS_UP = "HEADS_UP"
REPEAT = "REPEAT"


def heads_up_at(due_dt, work_schedule, today, hour):
    """The latest non-work day strictly before the due date, at `hour`:00.

    Walks back from the day before the deadline toward today and takes the
    first free day it finds -- the latest free evening. A shift blocks the
    whole day, not just its hours.

    If every day in that range is a work day, falls back to the day before
    the deadline. Khellon works three days a week so this should not fire;
    it exists so the system is never silent.
    """
    day = due_dt.date() - timedelta(days=1)
    while day >= today:
        if not is_work_day(day, work_schedule):
            return datetime(day.year, day.month, day.day, hour, tzinfo=due_dt.tzinfo)
        day -= timedelta(days=1)
    fallback = due_dt.date() - timedelta(days=1)
    return datetime(fallback.year, fallback.month, fallback.day, hour,
                    tzinfo=due_dt.tzinfo)


def current_stage(now, due_dt, heads_up, escalation_hours=(5, 2, 1), repeat_minutes=30):
    """Which single stage is active at `now`, or None if nothing is yet due.

    The windows partition the timeline, so a stage missed while the VM was
    down is never sent late and stale -- the stage that is current right now
    is sent instead.
    """
    if now >= due_dt - timedelta(minutes=repeat_minutes):
        return REPEAT
    for hours in sorted(escalation_hours):          # nearest deadline first
        if now >= due_dt - timedelta(hours=hours):
            return f"T{hours}H"
    first_escalation = due_dt - timedelta(hours=max(escalation_hours))
    if heads_up <= now and heads_up < first_escalation:
        return HEADS_UP
    return None


def in_quiet_hours(hour, quiet):
    """`quiet` is [start_hour, end_hour); handles a window that wraps midnight."""
    lo, hi = quiet
    return lo <= hour < hi if lo < hi else (hour >= lo or hour < hi)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python test_due.py`
Expected: `11/11 passed`

- [ ] **Step 5: Commit**

```bash
git add due.py test_due.py
git commit -m "feat: heads-up, escalation and quiet-hour scheduling rules"
```

---

### Task 4: SQLite store and feed reconciliation

**Files:**
- Modify: `due.py`
- Modify: `test_due.py`

**Interfaces:**
- Consumes: `due.HERE`
- Produces:
  - `due.DB_PATH: pathlib.Path` — `HERE / "due.db"`
  - `due.connect(path=None) -> sqlite3.Connection` — creates the schema if absent, `row_factory = sqlite3.Row`
  - `due.sync(db, events) -> None` — `events` is the `parse_ics` output
  - `due.mark_done(db, uid, when: datetime | None) -> None` — `None` un-marks
  - `due.pending(db) -> list[sqlite3.Row]` — active, not done

- [ ] **Step 1: Write the failing test**

Add to `test_due.py`:

```python
def test_sync_inserts_updates_and_deactivates():
    db = due.connect(":memory:")
    a = datetime(2026, 9, 10, 23, 59, tzinfo=TZ)
    due.sync(db, [("u1", "Quiz 1", a), ("u2", "Quiz 2", a)])
    assert len(due.pending(db)) == 2

    # u2 vanishes from the feed -> deactivated, not deleted
    due.sync(db, [("u1", "Quiz 1 renamed", a)])
    rows = {r["uid"]: r for r in db.execute("SELECT * FROM assignments")}
    assert rows["u1"]["title"] == "Quiz 1 renamed"
    assert rows["u2"]["active"] == 0
    assert len(due.pending(db)) == 1


def test_changed_due_date_rearms_the_schedule():
    db = due.connect(":memory:")
    a = datetime(2026, 9, 10, 23, 59, tzinfo=TZ)
    due.sync(db, [("u1", "Quiz", a)])
    db.execute("INSERT INTO sent (uid, stage, sent_at) VALUES ('u1','T5H','x')")
    db.execute("UPDATE assignments SET last_repeat_at='x' WHERE uid='u1'")
    db.commit()

    due.sync(db, [("u1", "Quiz", a + timedelta(days=7))])
    assert db.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 0, "sent rows cleared"
    row = db.execute("SELECT * FROM assignments WHERE uid='u1'").fetchone()
    assert row["last_repeat_at"] is None
    assert row["due"] == (a + timedelta(days=7)).isoformat()


def test_empty_feed_does_not_deactivate_everything():
    db = due.connect(":memory:")
    a = datetime(2026, 9, 10, 23, 59, tzinfo=TZ)
    due.sync(db, [("u1", "Quiz", a)])
    due.sync(db, [])
    assert len(due.pending(db)) == 1, "a failed or empty parse must not wipe the schedule"


def test_done_survives_a_moved_deadline():
    db = due.connect(":memory:")
    a = datetime(2026, 9, 10, 23, 59, tzinfo=TZ)
    due.sync(db, [("u1", "Quiz", a)])
    due.mark_done(db, "u1", a)
    moved = a + timedelta(days=7)
    due.sync(db, [("u1", "Quiz", moved)])
    row = db.execute("SELECT * FROM assignments WHERE uid='u1'").fetchone()
    assert row["done_at"] is not None, "a moved deadline must not resurrect a done assignment"
    assert row["due"] == moved.isoformat(), "the new deadline is still stored"
    assert due.pending(db) == [], "and it stays out of the pending list"


def test_mark_done_and_undo():
    db = due.connect(":memory:")
    a = datetime(2026, 9, 10, 23, 59, tzinfo=TZ)
    due.sync(db, [("u1", "Quiz", a)])
    due.mark_done(db, "u1", a)
    assert due.pending(db) == []
    due.mark_done(db, "u1", None)
    assert len(due.pending(db)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python test_due.py`
Expected: FAIL — `AttributeError: module 'due' has no attribute 'connect'`

- [ ] **Step 3: Write minimal implementation**

Add to `due.py` (`import sqlite3` at the top):

```python
DB_PATH = HERE / "due.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
  uid            TEXT PRIMARY KEY,
  title          TEXT NOT NULL,
  due            TEXT NOT NULL,
  active         INTEGER NOT NULL DEFAULT 1,
  done_at        TEXT,
  last_repeat_at TEXT
);
CREATE TABLE IF NOT EXISTS sent (
  uid     TEXT NOT NULL,
  stage   TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY (uid, stage)
);
"""


def connect(path=None):
    db = sqlite3.connect(str(path or DB_PATH))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def sync(db, events):
    """Reconcile the database against the feed."""
    seen = set()
    for uid, title, due_dt in events:
        seen.add(uid)
        due_s = due_dt.isoformat()
        row = db.execute("SELECT due FROM assignments WHERE uid=?", (uid,)).fetchone()
        if row is None:
            db.execute("INSERT INTO assignments (uid, title, due) VALUES (?,?,?)",
                       (uid, title, due_s))
        elif row["due"] != due_s:
            # Deadline moved: re-arm every stage against the new one.
            db.execute("UPDATE assignments SET title=?, due=?, active=1, "
                       "last_repeat_at=NULL WHERE uid=?", (title, due_s, uid))
            db.execute("DELETE FROM sent WHERE uid=?", (uid,))
        else:
            db.execute("UPDATE assignments SET title=?, active=1 WHERE uid=?", (title, uid))
    if seen:
        # Guarded: an empty list means the fetch failed, not that the semester
        # ended. Deactivating everything there would silence the system.
        placeholders = ",".join("?" * len(seen))
        db.execute(f"UPDATE assignments SET active=0 WHERE uid NOT IN ({placeholders})",
                   tuple(seen))
    db.commit()


def mark_done(db, uid, when):
    db.execute("UPDATE assignments SET done_at=? WHERE uid=?",
               (when.isoformat() if when else None, uid))
    db.commit()


def pending(db):
    return db.execute(
        "SELECT * FROM assignments WHERE active=1 AND done_at IS NULL ORDER BY due"
    ).fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python test_due.py`
Expected: `16/16 passed`

- [ ] **Step 5: Commit**

```bash
git add due.py test_due.py
git commit -m "feat: sqlite store and feed reconciliation"
```

---

### Task 5: Notification text and the ntfy sender

**Files:**
- Modify: `due.py`
- Modify: `test_due.py`

**Interfaces:**
- Consumes: `due.HEADS_UP`, `due.REPEAT`
- Produces:
  - `due.format_when(dt: datetime) -> str`
  - `due.notification(stage, title, due_dt, now) -> tuple[str, str, int, str]` — `(headline, body, priority, tags)`
  - `due.done_url(cfg, uid) -> str`
  - `due.send(cfg, headline, body, priority, tags, action_url=None) -> None`

- [ ] **Step 1: Write the failing test**

Add to `test_due.py`:

```python
def test_notification_text():
    now = datetime(2026, 9, 9, 20, 0, tzinfo=TZ)
    head, body, prio, _ = due.notification(due.HEADS_UP, "Chapter 2 Quiz", DUE, now)
    assert head == "Due tomorrow: Chapter 2 Quiz", head
    assert body == "Thu Sep 10, 11:59 PM", body
    assert prio == 3

    # Heads-up pushed two days back by a work shift must not say "tomorrow".
    head, _, _, _ = due.notification(due.HEADS_UP, "Chapter 2 Quiz", DUE,
                                     datetime(2026, 9, 8, 20, 0, tzinfo=TZ))
    assert head == "Due in 2 days: Chapter 2 Quiz", head

    head, _, prio, _ = due.notification("T1H", "Chapter 2 Quiz", DUE,
                                        DUE - timedelta(hours=1))
    assert head == "1 hour left: Chapter 2 Quiz", head
    assert prio == 5

    head, _, _, _ = due.notification("T5H", "Chapter 2 Quiz", DUE, DUE - timedelta(hours=5))
    assert head == "5 hours left: Chapter 2 Quiz", head

    head, _, _, _ = due.notification(due.REPEAT, "Chapter 2 Quiz", DUE,
                                     DUE - timedelta(minutes=30))
    assert head == "DUE SOON: Chapter 2 Quiz", head

    head, _, _, _ = due.notification(due.REPEAT, "Chapter 2 Quiz", DUE,
                                     DUE + timedelta(hours=9))
    assert head == "OVERDUE: Chapter 2 Quiz", head


def test_notification_time_format_is_platform_independent():
    # Built from fields rather than strftime %-I, which is not portable.
    afternoon = datetime(2026, 9, 17, 16, 28, tzinfo=TZ)
    _, body, _, _ = due.notification("T2H", "Unit I Assignment", afternoon, afternoon)
    assert body == "Thu Sep 17, 4:28 PM", body
    midnight = datetime(2026, 9, 17, 0, 5, tzinfo=TZ)
    _, body, _, _ = due.notification("T2H", "x", midnight, midnight)
    assert body == "Thu Sep 17, 12:05 AM", body


def test_send_folds_the_headline_and_builds_the_done_action():
    captured = {}

    class FakeResponse:
        def read(self):
            return b""
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def spy(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = req.data
        return FakeResponse()

    real_urlopen = due.urllib.request.urlopen
    due.urllib.request.urlopen = spy
    try:
        cfg = {"ntfy_server": "https://ntfy.sh/", "ntfy_topic": "secret-topic"}
        body = "Thu Sep 10, 11:59 PM — don't forget"
        due.send(cfg, "1 hour left: Chapter 2’s Quiz – Unit 1", body, 5,
                 "warning", action_url="http://1.2.3.4:8080/t/tok/done/u1")
    finally:
        due.urllib.request.urlopen = real_urlopen

    assert captured["url"] == "https://ntfy.sh/secret-topic", "trailing slash is stripped"
    assert captured["headers"]["title"] == "1 hour left: Chapter 2?s Quiz ? Unit 1"
    assert captured["headers"]["priority"] == "5"
    assert captured["headers"]["tags"] == "warning"
    assert captured["headers"]["actions"] == (
        "http, Done, http://1.2.3.4:8080/t/tok/done/u1, method=POST, clear=true")
    assert captured["body"] == body.encode("utf-8"), "the body must keep full UTF-8"


def test_done_url():
    cfg = {"base_url": "http://1.2.3.4:8080", "web_token": "abc123"}
    assert due.done_url(cfg, "_bb.GradableItem-_1436930_1") == \
        "http://1.2.3.4:8080/t/abc123/done/_bb.GradableItem-_1436930_1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python test_due.py`
Expected: FAIL — `AttributeError: module 'due' has no attribute 'notification'`

- [ ] **Step 3: Write minimal implementation**

Add to `due.py` (`from urllib.parse import quote` at the top):

```python
PRIORITY = {HEADS_UP: 3, "T5H": 3, "T2H": 4, "T1H": 5, REPEAT: 5}
TAGS = {HEADS_UP: "books", "T5H": "hourglass", "T2H": "hourglass_flowing_sand",
        "T1H": "warning", REPEAT: "rotating_light"}


def format_when(dt):
    """'Thu Sep 10, 11:59 PM'. Built from fields because strftime's %-I
    padding flag is not portable across libc implementations."""
    return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M %p}"


def notification(stage, title, due_dt, now):
    """(headline, body, priority, tags) for one notification."""
    if stage == HEADS_UP:
        days = (due_dt.date() - now.date()).days
        word = "tomorrow" if days == 1 else f"in {days} days"
        head = f"Due {word}: {title}"
    elif stage == REPEAT:
        head = f"{'OVERDUE' if now >= due_dt else 'DUE SOON'}: {title}"
    else:
        hours = int(stage[1:-1])
        head = f"{hours} hour{'s' if hours != 1 else ''} left: {title}"
    return head, format_when(due_dt), PRIORITY.get(stage, 4), TAGS.get(stage, "hourglass")


def done_url(cfg, uid):
    return f"{cfg['base_url']}/t/{cfg['web_token']}/done/{quote(uid, safe='')}"


def send(cfg, headline, body, priority, tags, action_url=None):
    """POST one notification to ntfy.

    ntfy headers must be ASCII, so the headline is folded; the body carries
    full UTF-8. Assignment titles from this feed are ASCII in practice.
    """
    headers = {
        "Title": headline.encode("ascii", "replace").decode("ascii"),
        "Priority": str(priority),
        "Tags": tags,
    }
    if action_url:
        headers["Actions"] = f"http, Done, {action_url}, method=POST, clear=true"
    req = urllib.request.Request(
        f"{cfg['ntfy_server'].rstrip('/')}/{cfg['ntfy_topic']}",
        data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()
```

Note `quote(uid, safe='')` — the `done_url` test expects the Blackboard UID to
pass through unchanged, which it does because `_`, `.` and `-` are unreserved.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python test_due.py`
Expected: `20/20 passed`

- [ ] **Step 5: Commit**

```bash
git add due.py test_due.py
git commit -m "feat: notification text and ntfy sender"
```

---

### Task 6: The tick

Wires everything together. Sends first and records second, so a failed send is retried on the next tick rather than being silently swallowed.

**Files:**
- Modify: `due.py`
- Modify: `test_due.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5
- Produces:
  - `due.tick(cfg=None, db=None, now=None, sender=None) -> list[tuple[str, str]]` — returns `(uid, stage)` for each notification sent, for testing
  - `python due.py tick` command line entry point

- [ ] **Step 1: Write the failing test**

Add to `test_due.py`:

```python
def _cfg():
    cfg = due.load_config(due.HERE / "config.example.json")
    cfg["work_schedule"] = dict(FREE)
    return cfg


def _armed(now_due=DUE):
    db = due.connect(":memory:")
    due.sync(db, [("u1", "Chapter 2 Quiz", now_due)])
    return db


def test_tick_sends_each_one_shot_stage_exactly_once():
    db, sent = _armed(), []
    at = datetime(2026, 9, 9, 20, 0, tzinfo=TZ)
    assert due.tick(_cfg(), db, at, sent.append) == [("u1", due.HEADS_UP)]
    assert due.tick(_cfg(), db, at + timedelta(minutes=5), sent.append) == []
    assert due.tick(_cfg(), db, DUE - timedelta(hours=5), sent.append) == [("u1", "T5H")]
    assert len(sent) == 2


def test_tick_repeats_every_28_minutes_and_respects_quiet_hours():
    db, sent = _armed(), []
    t = DUE - timedelta(minutes=30)
    assert due.tick(_cfg(), db, t, sent.append) == [("u1", due.REPEAT)]
    assert due.tick(_cfg(), db, t + timedelta(minutes=5), sent.append) == [], "too soon"
    assert due.tick(_cfg(), db, t + timedelta(minutes=29), sent.append) == [("u1", due.REPEAT)]
    # 00:00-08:00 is quiet, so nothing lands overnight...
    assert due.tick(_cfg(), db, DUE + timedelta(hours=3), sent.append) == []
    # ...and it resumes at 08:00.
    assert due.tick(_cfg(), db, DUE + timedelta(hours=9), sent.append) == [("u1", due.REPEAT)]


def test_quiet_hours_do_not_consume_the_repeat_slot():
    db, sent = _armed(), []
    t = DUE - timedelta(minutes=30)
    assert due.tick(_cfg(), db, t, sent.append) == [("u1", due.REPEAT)]
    stamp = db.execute("SELECT last_repeat_at FROM assignments WHERE uid='u1'").fetchone()[0]
    assert due.tick(_cfg(), db, DUE + timedelta(hours=3), sent.append) == [], "quiet hours send nothing"
    after = db.execute("SELECT last_repeat_at FROM assignments WHERE uid='u1'").fetchone()[0]
    assert after == stamp, "a suppressed repeat must not reset the pacing timer"

def test_tick_survives_a_feed_refresh_failure():
    db, sent = _armed(), []
    real_connect, real_fetch = due.connect, due.fetch_ics

    def boom(*args, **kwargs):
        raise OSError("feed unreachable")

    due.connect = lambda *a, **k: db
    due.fetch_ics = boom
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            fired = due.tick(_cfg(), None, datetime(2026, 9, 9, 20, 0, tzinfo=TZ), sent.append)
    finally:
        due.connect, due.fetch_ics = real_connect, real_fetch
    assert fired == [("u1", due.HEADS_UP)], "a dead feed must not stop notifications"
    assert "feed refresh failed" in stderr.getvalue(), "and it must say so on stderr"


def test_tick_is_silent_once_marked_done():
    db, sent = _armed(), []
    due.mark_done(db, "u1", DUE - timedelta(days=1))
    for at in (DUE - timedelta(hours=5), DUE, DUE + timedelta(days=1)):
        assert due.tick(_cfg(), db, at, sent.append) == []
    assert sent == []


def test_tick_does_not_record_a_failed_send():
    db = _armed()

    def boom(*a, **kw):
        raise OSError("ntfy unreachable")

    at = datetime(2026, 9, 9, 20, 0, tzinfo=TZ)
    assert due.tick(_cfg(), db, at, boom) == []
    assert db.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 0
    # The next tick retries it.
    sent = []
    assert due.tick(_cfg(), db, at + timedelta(minutes=5), sent.append) == [("u1", due.HEADS_UP)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python test_due.py`
Expected: FAIL — `AttributeError: module 'due' has no attribute 'tick'`

- [ ] **Step 3: Write minimal implementation**

Add to `due.py` (`import sys` at the top):

```python
def tick(cfg=None, db=None, now=None, sender=None):
    """One scheduling pass. Returns the (uid, stage) pairs actually sent.

    `db`, `now` and `sender` are injectable so the whole pass is testable
    against a fixed clock with no network and no files.
    """
    cfg = cfg if cfg is not None else load_config()
    tz = ZoneInfo(cfg["timezone"])
    now = now or datetime.now(tz)
    owns_db = db is None
    db = db if db is not None else connect()
    sender = sender or (lambda payload: send(cfg, *payload[:4], action_url=payload[4]))

    if owns_db:
        try:
            sync(db, parse_ics(fetch_ics(cfg), tz))
        except Exception as e:
            print(f"feed refresh failed, using cached data: {e}", file=sys.stderr)

    fired = []
    for row in pending(db):
        due_dt = datetime.fromisoformat(row["due"])
        hu = heads_up_at(due_dt, cfg["work_schedule"], now.date(), cfg["heads_up_hour"])
        stage = current_stage(now, due_dt, hu,
                              cfg["escalation_hours"], cfg["repeat_minutes"])
        if stage is None:
            continue

        if stage == REPEAT:
            if in_quiet_hours(now.hour, cfg["quiet_hours"]):
                continue
            last = row["last_repeat_at"]
            # Two minutes of slack absorbs five-minute cron jitter, so the
            # interval does not drift out to 35 minutes.
            gap = timedelta(minutes=cfg["repeat_minutes"] - 2)
            if last and now - datetime.fromisoformat(last) < gap:
                continue
        elif db.execute("SELECT 1 FROM sent WHERE uid=? AND stage=?",
                        (row["uid"], stage)).fetchone():
            continue

        payload = (*notification(stage, row["title"], due_dt, now),
                   done_url(cfg, row["uid"]))
        try:
            sender(payload)
        except Exception as e:
            print(f"send failed for {row['uid']} {stage}: {e}", file=sys.stderr)
            continue  # not recorded, so the next tick retries

        if stage == REPEAT:
            db.execute("UPDATE assignments SET last_repeat_at=? WHERE uid=?",
                       (now.isoformat(), row["uid"]))
        else:
            db.execute("INSERT INTO sent (uid, stage, sent_at) VALUES (?,?,?)",
                       (row["uid"], stage, now.isoformat()))
        db.commit()
        fired.append((row["uid"], stage))
    return fired


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "tick":
        for uid, stage in tick():
            print(f"sent {stage} for {uid}")
    else:
        sys.exit("usage: python due.py tick")
```

Note the test passes `sent.append` as `sender`, which receives the single
`payload` tuple — that is why `sender` takes one argument and the default
lambda unpacks it.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python test_due.py`
Expected: `26/26 passed`

- [ ] **Step 5: Commit**

```bash
git add due.py test_due.py
git commit -m "feat: tick orchestration with retry on send failure"
```

---

### Task 7: The web page

**Files:**
- Create: `web.py`
- Modify: `test_due.py`

**Interfaces:**
- Consumes: `due.load_config`, `due.save_config`, `due.connect`, `due.mark_done`, `due.DAY_NAMES`, `due.format_when`
- Produces:
  - `web.app: Flask`
  - `web.validate_schedule(raw: str) -> dict` — raises `ValueError` with a human-readable message

- [ ] **Step 1: Write the failing test**

Add to `test_due.py` (add `import web` beside `import due`):

```python
def test_validate_schedule_accepts_good_input():
    got = web.validate_schedule('{"Mon": ["16:00-21:00"], "Tue": []}')
    assert got == {"Mon": ["16:00-21:00"], "Tue": []}, got
    assert web.validate_schedule('{"Sat": ["22:00-06:00"]}') == {"Sat": ["22:00-06:00"]}


def test_validate_schedule_rejects_bad_input():
    bad = [
        ("not json at all", "json"),
        ('["Mon"]', "object"),
        ('{"Monday": []}', "unknown day"),
        ('{"Mon": "16:00-21:00"}', "list"),
        ('{"Mon": ["4pm to 9pm"]}', "HH:MM-HH:MM"),
        ('{"Mon": ["99:99-99:99"]}', "HH:MM-HH:MM"),
        ('{"Mon": ["24:00-25:00"]}', "HH:MM-HH:MM"),
    ]
    for raw, hint in bad:
        try:
            web.validate_schedule(raw)
        except ValueError as e:
            assert hint.lower() in str(e).lower(), f"{raw}: unhelpful message {e!r}"
        else:
            raise AssertionError(f"accepted bad schedule: {raw}")


def test_web_requires_the_token():
    client = web.app.test_client()
    assert client.get("/").status_code == 404
    assert client.get("/t/wrong-token/").status_code == 404

def test_web_guards_every_mutating_route():
    # index is covered above; these three CHANGE state, so a dropped guard here
    # is the serious one. Each must 404 before touching the db or the config.
    client = web.app.test_client()
    for path in ("/t/wrong-token/done/u1",
                 "/t/wrong-token/undone/u1",
                 "/t/wrong-token/schedule"):
        assert client.post(path).status_code == 404, f"unguarded: {path}"

```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python test_due.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'web'`

- [ ] **Step 3: Write minimal implementation**

Create `web.py`:

```python
"""Private web page: mark assignments done, edit the work schedule.

Access control is the token in the URL path. Anything else 404s.
"""
import hmac
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, render_template_string, request

import due

app = Flask(__name__)

SHIFT = re.compile(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d")


def validate_schedule(raw):
    """Parse and check a posted work schedule. Raises ValueError if unusable."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid json: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("must be a json object mapping days to shift lists")
    out = {}
    for day, shifts in data.items():
        if day not in due.DAY_NAMES:
            raise ValueError(f"unknown day {day!r}, expected one of {', '.join(due.DAY_NAMES)}")
        if not isinstance(shifts, list):
            raise ValueError(f"{day} must be a list of shifts, got {type(shifts).__name__}")
        for s in shifts:
            if not SHIFT.fullmatch(str(s)):
                raise ValueError(f"bad shift {s!r} on {day}, expected HH:MM-HH:MM")
        out[day] = [str(s) for s in shifts]
    return out


def _guard(token):
    if not hmac.compare_digest(token, due.load_config()["web_token"]):
        abort(404)


@app.get("/t/<token>/")
def index(token):
    _guard(token)
    cfg = due.load_config()
    tz = ZoneInfo(cfg["timezone"])
    db = due.connect()
    cutoff = (datetime.now(tz) - timedelta(days=3)).isoformat()
    rows = db.execute(
        "SELECT * FROM assignments WHERE active=1 AND due > ? "
        "ORDER BY done_at IS NOT NULL, due", (cutoff,)).fetchall()
    items = [{
        "uid": r["uid"],
        "title": r["title"],
        "when": due.format_when(datetime.fromisoformat(r["due"])),
        "done": r["done_at"] is not None,
    } for r in rows]
    return render_template_string(
        PAGE, items=items, token=token, error=request.args.get("error"),
        schedule=json.dumps(cfg["work_schedule"], indent=2))


@app.post("/t/<token>/done/<path:uid>")
def done(token, uid):
    _guard(token)
    cfg = due.load_config()
    due.mark_done(due.connect(), uid, datetime.now(ZoneInfo(cfg["timezone"])))
    # ntfy's action button wants a plain 200; a browser form wants the page.
    if request.accept_mimetypes.accept_html:
        return redirect(f"/t/{token}/")
    return "done", 200


@app.post("/t/<token>/undone/<path:uid>")
def undone(token, uid):
    _guard(token)
    due.mark_done(due.connect(), uid, None)
    return redirect(f"/t/{token}/")


@app.post("/t/<token>/schedule")
def schedule(token):
    _guard(token)
    try:
        parsed = validate_schedule(request.form["schedule"])
    except ValueError as e:
        return redirect(f"/t/{token}/?error={e}")
    cfg = due.load_config()
    cfg["work_schedule"] = parsed
    due.save_config(cfg)
    return redirect(f"/t/{token}/")


PAGE = """<!doctype html>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Assignments</title>
<style>
 body{font:16px/1.5 system-ui;margin:0;padding:1rem;max-width:40rem}
 li{list-style:none;display:flex;gap:.75rem;align-items:center;
    padding:.6rem 0;border-bottom:1px solid #ddd}
 li.done{opacity:.45}
 .t{flex:1}.w{color:#666;font-size:.85em}
 button{padding:.5rem .9rem;font-size:1rem;border-radius:.4rem;
        border:1px solid #888;background:#fff}
 textarea{width:100%;height:11rem;font:14px ui-monospace}
 .err{background:#fee;border:1px solid #c00;padding:.5rem;border-radius:.4rem}
</style>
<h1>Assignments</h1>
{% if error %}<p class=err>{{ error }}</p>{% endif %}
<ul>
{% for i in items %}
  <li class="{{ 'done' if i.done }}">
    <span class=t>{{ i.title }}<br><span class=w>{{ i.when }}</span></span>
    <form method=post action="/t/{{ token }}/{{ 'undone' if i.done else 'done' }}/{{ i.uid }}">
      <button>{{ 'Undo' if i.done else 'Done' }}</button>
    </form>
  </li>
{% else %}
  <li>Nothing upcoming.</li>
{% endfor %}
</ul>
<h2>Work schedule</h2>
<form method=post action="/t/{{ token }}/schedule">
  <textarea name=schedule>{{ schedule }}</textarea>
  <button>Save</button>
</form>
"""
```

- [ ] **Step 4: Run test to verify it passes**

`test_web_requires_the_token` calls `due.load_config()`, which reads
`config.json`. Create it first if you have not already:

```bash
cp -n config.example.json config.json
```

Run: `.venv/bin/python test_due.py`
Expected: `30/30 passed`

- [ ] **Step 5: Commit**

```bash
git add web.py test_due.py
git commit -m "feat: web page for marking done and editing the work schedule"
```

---

### Task 8: Deployment

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: everything
- Produces: no code

- [ ] **Step 1: Write `README.md`**

````markdown
# Assignment due notifier

Watches the Blackboard calendar feed and pushes escalating notifications to
your phone until you mark each assignment done.

## Notification schedule

For something due Thursday at 11:59 PM:

| When | What |
|---|---|
| 8:00 PM on the latest free evening before the due date | "Due tomorrow" |
| 6:59 PM Thursday | 5 hours left |
| 9:59 PM Thursday | 2 hours left |
| 10:59 PM Thursday | 1 hour left |
| 11:29 PM Thursday onward | every 30 minutes, continuing past the deadline |
| midnight - 8 AM | quiet |

A work shift blocks the whole day, so the heads-up moves back to the last free
evening. Tapping **Done** stops everything for that item.

## Phone setup

1. Install the **ntfy** app (iOS / Android / F-Droid).
2. Subscribe to the topic name you put in `config.json` as `ntfy_topic`.
   The topic name is the only secret, so make it long and random.

## VM setup (Ubuntu 24.04)

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <this repo> ~/due && cd ~/due
python3 -m venv .venv && .venv/bin/pip install icalendar flask waitress
cp config.example.json config.json
```

Generate the two secrets and put them in `config.json`:

```bash
python3 -c "import secrets; print('ntfy_topic:', secrets.token_urlsafe(24)); print('web_token:', secrets.token_hex(16))"
```

Then set `base_url` to `http://<your VM public IP>:8080` and fill in
`work_schedule`. Verify:

```bash
.venv/bin/python test_due.py
.venv/bin/python due.py tick
```

## First run: clear the backlog BEFORE enabling cron

Anything already past its deadline is treated as still outstanding, so the
first tick will notify you about all of it at once — at the time of writing
that is 12 assignments, at urgent priority, repeating every 30 minutes until
you mark them done.

So do this in order:

1. Start the web page (next section) and open it on your phone.
2. Tap **Done** on everything you have already handed in.
3. Only then enable cron, below.

If you skip this you will get a wall of notifications and then have to clear
them from the page anyway.

## Cron

```bash
crontab -e
```

```
*/5 * * * * cd ~/due && .venv/bin/python due.py tick >> tick.log 2>&1
```

## Web page as a service

```bash
sudo tee /etc/systemd/system/due-web.service >/dev/null <<'EOF'
[Unit]
Description=Assignment due notifier web page
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/due
ExecStart=/home/ubuntu/due/.venv/bin/waitress-serve --host 0.0.0.0 --port 8080 web:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now due-web
```

## Firewall — both layers

This is where Oracle VMs usually appear dead from the internet. **Both** of
these are required.

1. **Oracle Cloud console:** Networking > Virtual Cloud Networks > your VCN >
   Security Lists > Default > Add Ingress Rule. Source `0.0.0.0/0`, IP
   protocol TCP, destination port `8080`.

2. **On the VM.** Ubuntu images on Oracle ship iptables rules that drop
   everything, and they survive reboots, so the rule must be inserted and
   persisted:

```bash
sudo iptables -I INPUT 6 -p tcp --dport 8080 -j ACCEPT
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

Confirm from your phone: `http://<vm-ip>:8080/t/<web_token>/`

## Log rotation

```bash
sudo tee /etc/logrotate.d/due >/dev/null <<'EOF'
/home/ubuntu/due/tick.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
EOF
```

## Changing your work schedule

Edit it on the web page. Days with a shift are work days; days with an empty
list are free. Changes take effect on the next tick, within five minutes.

## Upgrading to HTTPS

The page is plain HTTP protected by the token in the URL. To encrypt it,
install Tailscale on the VM and your phone, set `base_url` to the VM's
Tailscale name, and remove the two firewall rules above. No domain needed.
````

- [ ] **Step 2: Verify against a real run, WITHOUT sending anything**

A first tick against the live feed fires a notification for every assignment
already past its deadline — 12 of them at the time of writing, all at urgent
priority. `config.example.json` ships a placeholder `ntfy_topic`, and ntfy
topics are public, so a verification run must not be allowed to publish.

On the VM this runs as written. On a macOS dev box the python.org build ships
no CA bundle (`ssl.get_default_verify_paths().cafile` is `None`), so `urllib`
cannot verify the Blackboard certificate and the fetch fails — Ubuntu 24.04's
Python reads `/etc/ssl/certs` and is unaffected. Seed the cache with `curl`
first so the parse, the sync and the stage machine are still exercised for real
and only the TLS handshake is skipped:

```bash
curl -sL "$(python3 -c 'import json;print(json.load(open("config.example.json"))["ics_url"])')" -o learn.ics
ls -l learn.ics    # expect ~15 KB
```

Point ntfy at an unroutable address next, so the feed fetch, the database sync
and the stage machine all run for real while every send fails locally:

```bash
cp -n config.example.json config.json
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("config.json")
cfg = json.loads(p.read_text())
cfg["ntfy_server"] = "http://127.0.0.1:9"   # discard port, nothing leaves the box
p.write_text(json.dumps(cfg, indent=2))
EOF
```

Run: `.venv/bin/python test_due.py && .venv/bin/python due.py tick`
Expected: `30/30 passed`, then one `send failed for ... Connection refused` line
per overdue assignment on stderr and no `sent` output. Those failures are the
correct behaviour — a failed send is not recorded, so the next tick retries.

Then confirm the feed really was parsed and stored:

Run: `sqlite3 due.db "SELECT COUNT(*) FROM assignments;"`
Expected: `49`

Then delete the scratch state so the VM starts clean:

Run: `rm -f due.db learn.ics config.json`

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: VM deployment, both firewall layers, and log rotation"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Data source, escapes and folding | 2 |
| ntfy channel, action button | 5 |
| Stages and windows | 3 |
| `heads_up_at`, whole-day rule, fallback, degenerate case | 3 |
| Quiet hours | 3, 6 |
| Recomputation on every tick | 6 |
| Work schedule format | 1, 7 |
| Marking done, both paths | 4, 5, 7 |
| Web routes and token | 7 |
| Data model, reconciliation | 4 |
| Feed caching and failure fallback | 2 |
| Deployment, both firewall layers, logrotate | 8 |
| Testing list | 1-7 |

**Type consistency:** `due_dt` is the parameter name for a deadline everywhere
(`due` is the module name and must not be shadowed). Stage strings are
`HEADS_UP`, `T5H`/`T2H`/`T1H` derived as `f"T{hours}H"`, and `REPEAT`.
`sender` always takes exactly one packed payload tuple of
`(headline, body, priority, tags, action_url)`.

**Known simplification:** `sync` re-queries per event rather than batching.
At 49 rows every five minutes this is irrelevant; if the feed ever grows past
a few thousand items, replace it with a single `INSERT ... ON CONFLICT`.
