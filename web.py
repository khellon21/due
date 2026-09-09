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

SHIFT = re.compile(r"\d{2}:\d{2}-\d{2}:\d{2}")


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
