"""Self-check for due.py.  Run: python test_due.py"""
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import due

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
    try:
        cfg = {"ics_url": "http://127.0.0.1:1/never-reachable"}
        got = due.fetch_ics(cfg, max_age=3600, cache=scratch)
        assert got == FIXTURE, "fresh cache must not hit the network"
    finally:
        scratch.unlink()


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
