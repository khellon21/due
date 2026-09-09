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

SOON_DAYS = 7


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


def countdown(due_dt, now):
    """'in 22 hours' / 'in 5 days' / '2 days overdue'."""
    seconds = (due_dt - now).total_seconds()
    late = seconds < 0
    seconds = abs(seconds)
    if seconds < 3600:
        n, unit = max(1, int(seconds // 60)), "minute"
    elif seconds < 86400:
        n, unit = int(seconds // 3600), "hour"
    else:
        n, unit = int(seconds // 86400), "day"
    span = f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{span} overdue" if late else f"in {span}"


def _guard(token):
    if not hmac.compare_digest(token, due.load_config()["web_token"]):
        abort(404)


@app.get("/t/<token>/")
def index(token):
    _guard(token)
    cfg = due.load_config()
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    soon_until = now + timedelta(days=SOON_DAYS)
    db = due.connect()

    # Every active row, with no age cutoff. An overdue assignment keeps
    # notifying until it is marked done, so hiding the old ones left them
    # nagging with no way to switch them off.
    overdue, soon, later, done = [], [], [], []
    for r in db.execute("SELECT * FROM assignments WHERE active=1 ORDER BY due"):
        at = datetime.fromisoformat(r["due"])
        item = {
            "uid": r["uid"],
            "title": r["title"],
            "when": due.format_when(at),
            "countdown": countdown(at, now),
            "urgent": at - now < timedelta(days=1),
        }
        if r["done_at"] is not None:
            done.append(item)
        elif at < now:
            overdue.append(item)
        elif at <= soon_until:
            soon.append(item)
        else:
            later.append(item)
    done.reverse()

    total = len(overdue) + len(soon) + len(later) + len(done)
    return render_template_string(
        PAGE, token=token, error=request.args.get("error"),
        overdue=overdue, soon=soon, later=later, done=done,
        total=total, done_count=len(done),
        percent=round(100 * len(done) / total) if total else 0,
        next_up=(soon or later or [None])[0],
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
 :root{
   --bg:#f6f7f9; --card:#fff; --ink:#14171a; --dim:#697280; --line:#e3e6ea;
   --late:#c8322b; --lateBg:#fdf0ef; --soon:#b3610a; --ok:#1f8b4c;
 }
 @media (prefers-color-scheme:dark){:root{
   --bg:#14171a; --card:#1c2024; --ink:#e9edf1; --dim:#9099a5; --line:#2a3038;
   --late:#ff6b60; --lateBg:#2a1a19; --soon:#e2963a; --ok:#4cc38a;
 }}
 *{box-sizing:border-box}
 body{font:16px/1.55 -apple-system,system-ui,sans-serif;margin:0;padding:1rem;
      background:var(--bg);color:var(--ink);max-width:34rem;margin-inline:auto}
 h1{font-size:1.5rem;margin:.2rem 0 .1rem}
 .bar{height:.5rem;border-radius:.25rem;background:var(--line);overflow:hidden;margin:.7rem 0 .3rem}
 .bar i{display:block;height:100%;background:var(--ok);border-radius:.25rem}
 .sub{color:var(--dim);font-size:.85rem}
 h2{font-size:.75rem;letter-spacing:.09em;text-transform:uppercase;
    color:var(--dim);margin:1.6rem 0 .5rem}
 h2.late{color:var(--late)}
 .card{background:var(--card);border:1px solid var(--line);border-radius:.7rem;
       padding:.8rem .9rem;margin-bottom:.5rem;display:flex;gap:.8rem;align-items:center}
 .card.late{border-left:3px solid var(--late);background:var(--lateBg)}
 .card.urgent{border-left:3px solid var(--soon)}
 .card.off{opacity:.5}
 .t{flex:1;min-width:0}
 .t b{font-weight:600;display:block;overflow-wrap:anywhere}
 .m{font-size:.8rem;color:var(--dim);margin-top:.15rem}
 .m .c{font-weight:600;color:var(--ink)}
 .m .c.late{color:var(--late)} .m .c.urgent{color:var(--soon)}
 button{padding:.5rem .85rem;font:inherit;font-size:.85rem;border-radius:.5rem;
        border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
 button:hover{border-color:var(--dim)}
 .card.late button{border-color:var(--late);color:var(--late)}
 details{margin-top:.6rem;border-top:1px solid var(--line);padding-top:.6rem}
 summary{cursor:pointer;color:var(--dim);font-size:.9rem;padding:.35rem 0}
 textarea{width:100%;height:12rem;font:13px/1.5 ui-monospace,monospace;padding:.6rem;
          border-radius:.5rem;border:1px solid var(--line);background:var(--card);color:var(--ink)}
 .err{background:var(--lateBg);border:1px solid var(--late);color:var(--late);
      padding:.6rem .8rem;border-radius:.5rem;font-size:.9rem}
 .clear{text-align:center;padding:2.5rem 1rem;color:var(--dim)}
 .clear b{display:block;font-size:1.15rem;color:var(--ink);margin-bottom:.4rem}
</style>

<h1>Assignments</h1>
<div class=bar><i style="width:{{ percent }}%"></i></div>
<div class=sub>{{ done_count }} of {{ total }} done &middot; {{ percent }}%</div>

{% if error %}<p class=err>{{ error }}</p>{% endif %}

{% macro row(i, kind) %}
  <div class="card {{ kind }}{{ ' urgent' if kind == '' and i.urgent }}{{ ' off' if kind == 'off' }}">
    <span class=t>
      <b>{{ i.title }}</b>
      <span class=m>
        <span class="c {{ 'late' if kind == 'late' }}{{ ' urgent' if kind == '' and i.urgent }}">{{ i.countdown }}</span>
        &middot; {{ i.when }}
      </span>
    </span>
    <form method=post action="/t/{{ token }}/{{ 'undone' if kind == 'off' else 'done' }}/{{ i.uid }}">
      <button>{{ 'Undo' if kind == 'off' else 'Done' }}</button>
    </form>
  </div>
{% endmacro %}

{% if overdue %}
  <h2 class=late>Overdue &middot; {{ overdue|length }}</h2>
  {% for i in overdue %}{{ row(i, 'late') }}{% endfor %}
{% endif %}

{% if soon %}
  <h2>Due soon</h2>
  {% for i in soon %}{{ row(i, '') }}{% endfor %}
{% endif %}

{% if not overdue and not soon %}
  <div class=clear>
    <b>All clear.</b>
    {% if next_up %}Next up: {{ next_up.title }} &mdash; {{ next_up.countdown }}.
    {% else %}Nothing left this semester.{% endif %}
  </div>
{% endif %}

{% if later %}
<details>
  <summary>Later &middot; {{ later|length }}</summary>
  {% for i in later %}{{ row(i, '') }}{% endfor %}
</details>
{% endif %}

{% if done %}
<details>
  <summary>Done &middot; {{ done|length }}</summary>
  {% for i in done %}{{ row(i, 'off') }}{% endfor %}
</details>
{% endif %}

<details>
  <summary>Work schedule</summary>
  <form method=post action="/t/{{ token }}/schedule">
    <textarea name=schedule>{{ schedule }}</textarea>
    <button>Save</button>
  </form>
</details>
"""
