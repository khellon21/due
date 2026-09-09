"""Self-check for due.py.  Run: python test_due.py"""
import sys
from datetime import date, datetime, timedelta
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


def test_mark_done_and_undo():
    db = due.connect(":memory:")
    a = datetime(2026, 9, 10, 23, 59, tzinfo=TZ)
    due.sync(db, [("u1", "Quiz", a)])
    due.mark_done(db, "u1", a)
    assert due.pending(db) == []
    due.mark_done(db, "u1", None)
    assert len(due.pending(db)) == 1


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


def test_done_url():
    cfg = {"base_url": "http://1.2.3.4:8080", "web_token": "abc123"}
    assert due.done_url(cfg, "_bb.GradableItem-_1436930_1") == \
        "http://1.2.3.4:8080/t/abc123/done/_bb.GradableItem-_1436930_1"


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
