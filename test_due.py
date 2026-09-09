"""Self-check for due.py.  Run: python test_due.py"""
import contextlib
import io
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import due
import web

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
        cfg = {"ntfy_server": "https://ntfy.sh/", "ntfy_topic": "secret-topic",
               "base_url": "http://1.2.3.4:8080", "web_token": "tok"}
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
    assert captured["headers"]["click"] == "http://1.2.3.4:8080/t/tok/", \
        "tapping the notification body must open the page"
    assert captured["body"] == body.encode("utf-8"), "the body must keep full UTF-8"


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


def test_countdown_wording():
    now = datetime(2026, 9, 9, 12, 0, tzinfo=TZ)
    cases = [
        (now + timedelta(minutes=45), "in 45 minutes"),
        (now + timedelta(hours=22), "in 22 hours"),
        (now + timedelta(days=5), "in 5 days"),
        (now + timedelta(days=1), "in 1 day"),
        (now - timedelta(days=2), "2 days overdue"),
        (now - timedelta(hours=3), "3 hours overdue"),
        (now - timedelta(seconds=30), "1 minute overdue"),
    ]
    for at, expected in cases:
        assert web.countdown(at, now) == expected, f"{at}: got {web.countdown(at, now)!r}"


def test_page_shows_assignments_overdue_by_more_than_three_days():
    # Regression: the page used to filter to due > now-3d, which hid long-overdue
    # assignments while they kept notifying every 30 minutes -- no way to stop them.
    db = due.connect(":memory:")
    stale = datetime.now(TZ) - timedelta(days=13)
    due.sync(db, [("old-1", "Syllabee Activity", stale)])
    real_connect = due.connect
    due.connect = lambda *a, **k: db
    try:
        cfg = due.load_config()
        body = web.app.test_client().get(f"/t/{cfg['web_token']}/").data.decode()
    finally:
        due.connect = real_connect
    assert "Syllabee Activity" in body, "a long-overdue assignment must still be listed"
    assert "13 days overdue" in body


def test_xp_rewards_early_and_penalises_late():
    at = datetime(2026, 9, 10, 23, 59, tzinfo=TZ)
    assert web.xp_for(at, None) == 0, "nothing is scored until it is done"
    assert web.xp_for(at, at - timedelta(hours=2)) == 10, "same day, on time"
    assert web.xp_for(at, at - timedelta(days=3)) == 16, "10 + 3 days x 2"
    assert web.xp_for(at, at - timedelta(days=90)) == 50, "early bonus caps at +40"
    assert web.xp_for(at, at + timedelta(minutes=1)) == -5, "any lateness costs a day"
    assert web.xp_for(at, at + timedelta(days=3)) == -15
    assert web.xp_for(at, at + timedelta(days=99)) == -30, "late penalty has a floor"


def test_preexisting_backlog_is_never_scored():
    # Clearing a semester's imported backlog must not open the scoreboard deep
    # in the negative -- the notifier was not watching those deadlines.
    at = datetime(2026, 8, 27, 23, 59, tzinfo=TZ)
    first_seen = datetime(2026, 9, 9, 4, 48, tzinfo=TZ)   # tracking began later
    done_now = datetime(2026, 9, 9, 12, 0, tzinfo=TZ)
    assert web.xp_for(at, done_now, first_seen) == 0, "backlog is unscored"
    assert web.xp_for(at, done_now) == -30, "without first_seen it still penalises"
    # An assignment seen BEFORE its deadline scores normally.
    later = datetime(2026, 9, 20, 23, 59, tzinfo=TZ)
    assert web.xp_for(later, later - timedelta(days=2), first_seen) == 14


def test_rank_tiers():
    assert web.rank_for(0)["name"] == "Iron"
    assert web.rank_for(-40)["name"] == "Iron", "a negative total cannot rank below the floor"
    assert web.rank_for(-40)["pct"] == 0, "and shows no progress rather than negative"
    assert web.rank_for(74)["name"] == "Iron"
    assert web.rank_for(75)["name"] == "Bronze"
    top = web.rank_for(1300)
    assert (top["name"], top["next_name"], top["pct"]) == ("Radiant", None, 100)
    r = web.rank_for(125)
    assert (r["name"], r["next_name"], r["to_next"]) == ("Bronze", "Silver", 50)
    assert r["pct"] == 50, "125 is halfway from 75 to 175"
    assert [web.rank_for(t)["chevrons"] for t in (0, 300, 825)] == [1, 2, 3], \
        "chevrons step up every three tiers"
    assert all(len(r) == 3 for r in web.RANKS), "every tier carries a threshold, name and colour"


def test_donut_segments_cover_the_circle_with_gaps():
    segs = web._donut(3, 5, 2)
    assert [s["label"] for s in segs] == ["Done", "Upcoming", "Overdue"]
    assert [s["count"] for s in segs] == [3, 5, 2]
    assert [s["pct"] for s in segs] == [30, 50, 20]
    assert web._donut(0, 5, 0)[0]["label"] == "Upcoming", "empty categories are dropped"
    drawn = sum(float(s["dash"].split()[0]) for s in segs)
    assert web.CIRCUMFERENCE - 12 < drawn < web.CIRCUMFERENCE, "segments fill the ring minus gaps"


def test_schedule_form_builds_and_validates():
    form = {"on_Mon": "on", "start_Mon": "22:00", "end_Mon": "06:00",
            "on_Sat": "on", "start_Sat": "10:00", "end_Sat": "18:00"}
    got = web.schedule_from_form(form)
    assert got["Mon"] == ["22:00-06:00"], "overnight shift survives the form"
    assert got["Sat"] == ["10:00-18:00"]
    assert got["Tue"] == [] and got["Sun"] == [], "unticked days are free"
    try:
        web.schedule_from_form({"on_Wed": "on", "start_Wed": "", "end_Wed": ""})
    except ValueError as e:
        assert "no hours" in str(e), e
    else:
        raise AssertionError("a ticked day with no hours must be rejected")


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
