"""Blackboard assignment due-date notifier.

Run `python due.py tick` from cron every five minutes.
"""
import json
import pathlib
import urllib.request
import time
from datetime import datetime
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
