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
