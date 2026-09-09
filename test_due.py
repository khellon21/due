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
