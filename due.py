"""Blackboard assignment due-date notifier.

Run `python due.py tick` from cron every five minutes.
"""
import json
import pathlib
import sqlite3
import urllib.request
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar

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
