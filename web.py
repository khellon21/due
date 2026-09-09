"""Private web page: the assignment dashboard.

Access control is the token in the URL path. Anything else 404s.
"""
import hmac
import json
import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, render_template_string, request

import due

app = Flask(__name__)

SHIFT = re.compile(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d")
SOON_DAYS = 7

# Scoring. Finish early, gain; finish late, lose.
BASE_POINTS = 10          # for handing anything in on time
PER_DAY_EARLY = 2         # each full day of head start
EARLY_CAP = 40            # most you can earn from being early
PER_DAY_LATE = 5          # lost per day late
LATE_FLOOR = -30          # worst a single assignment can cost you

RANKS = [(0, "Freshman"), (100, "Sophomore"), (250, "Junior"),
         (450, "Senior"), (700, "Honor Roll"), (1000, "Dean's List")]

# Validated with the dataviz palette validator, both modes, all checks pass.
DONE_COLOR, UPCOMING_COLOR, OVERDUE_COLOR = "#0ca30c", "#2a78d6", "#d03b3b"
RADIUS = 52
CIRCUMFERENCE = 2 * math.pi * RADIUS


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


def schedule_from_form(form):
    """Build the schedule from the day/time controls, then validate it normally.

    The form is a convenience over the same JSON the notifier reads, so it goes
    through validate_schedule like anything else rather than trusting the browser.
    """
    out = {}
    for day in due.DAY_NAMES:
        if not form.get(f"on_{day}"):
            out[day] = []
            continue
        start, end = form.get(f"start_{day}", ""), form.get(f"end_{day}", "")
        if not start or not end:
            raise ValueError(f"{day} is ticked as a work day but has no hours")
        out[day] = [f"{start}-{end}"]
    return validate_schedule(json.dumps(out))


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


def points_for(due_dt, done_at, first_seen=None):
    """Points for one assignment. Nothing is scored until it is done.

    An assignment that was ALREADY overdue when the notifier first saw it is
    never scored, in either direction: the system was not watching it, so
    neither the credit nor the penalty is earned. Without this, importing a
    semester's backlog opens the scoreboard at a few hundred points negative.
    """
    if done_at is None:
        return 0
    if first_seen is not None and due_dt < first_seen:
        return 0
    days = (due_dt - done_at).total_seconds() / 86400
    if days >= 0:
        return BASE_POINTS + min(int(days) * PER_DAY_EARLY, EARLY_CAP)
    return max(-PER_DAY_LATE * math.ceil(-days), LATE_FLOOR)


def rank_for(points):
    """(current rank, next rank or None, points still needed, % toward next)."""
    earned = [r for r in RANKS if points >= r[0]]
    floor, name = earned[-1] if earned else RANKS[0]
    # Ranks above the CURRENT floor, not above the score: a negative score sits
    # below the first threshold, and comparing against it gives a zero-width span.
    later = [r for r in RANKS if r[0] > floor]
    if not later:
        return name, None, 0, 100
    ceiling, next_name = later[0]
    pct = max(0, round(100 * (points - floor) / (ceiling - floor)))
    return name, next_name, ceiling - points, pct


def _donut(done_n, upcoming_n, overdue_n):
    """Segments for the completion donut, as dash offsets around one circle."""
    total = done_n + upcoming_n + overdue_n
    segments, travelled = [], 0.0
    for label, count, color in (("Done", done_n, DONE_COLOR),
                                ("Upcoming", upcoming_n, UPCOMING_COLOR),
                                ("Overdue", overdue_n, OVERDUE_COLOR)):
        if not count:
            continue
        length = CIRCUMFERENCE * count / total
        drawn = max(length - 2, 1)          # 2px surface gap between segments
        segments.append({
            "color": color, "label": label, "count": count,
            "pct": round(100 * count / total),
            "dash": f"{drawn:.2f} {CIRCUMFERENCE - drawn:.2f}",
            "offset": f"{-travelled:.2f}",
        })
        travelled += length
    return segments


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
    points = 0
    for r in db.execute("SELECT * FROM assignments WHERE active=1 ORDER BY due"):
        at = datetime.fromisoformat(r["due"])
        finished = datetime.fromisoformat(r["done_at"]) if r["done_at"] else None
        seen = datetime.fromisoformat(r["first_seen"]) if r["first_seen"] else None
        backlog = seen is not None and at < seen
        scored = points_for(at, finished, seen)
        points += scored
        item = {
            "uid": r["uid"], "title": r["title"],
            "when": due.format_when(at),
            "countdown": countdown(at, now),
            "urgent": at - now < timedelta(days=1),
            "points": scored, "backlog": backlog,
        }
        if finished is not None:
            done.append(item)
        elif at < now:
            overdue.append(item)
        elif at <= soon_until:
            soon.append(item)
        else:
            later.append(item)
    done.reverse()

    total = len(overdue) + len(soon) + len(later) + len(done)
    rank, next_rank, to_next, rank_pct = rank_for(points)
    schedule = cfg["work_schedule"]
    days = []
    for d in due.DAY_NAMES:
        shifts = schedule.get(d) or []
        start, end = (shifts[0].split("-") if shifts else ("16:00", "21:00"))
        days.append({"day": d, "on": bool(shifts), "start": start, "end": end,
                     "extra": len(shifts) > 1})

    return render_template_string(
        PAGE, token=token, error=request.args.get("error"),
        overdue=overdue, soon=soon, later=later, done=done,
        total=total, done_count=len(done),
        percent=round(100 * len(done) / total) if total else 0,
        segments=_donut(len(done), len(soon) + len(later), len(overdue)),
        circumference=f"{CIRCUMFERENCE:.2f}", radius=RADIUS,
        points=points, rank=rank, next_rank=next_rank,
        to_next=to_next, rank_pct=rank_pct, ranks=RANKS,
        next_up=(soon or later or [None])[0],
        days=days, raw_schedule=json.dumps(schedule, indent=2))


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
        if "schedule" in request.form:
            parsed = validate_schedule(request.form["schedule"])
        else:
            parsed = schedule_from_form(request.form)
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
   --bg:#f2f3f5; --card:#fcfcfb; --ink:#0b0b0b; --dim:#52514e; --line:#e4e5e7;
   --done:#0ca30c; --up:#2a78d6; --late:#d03b3b; --lateBg:#fdf1f1; --track:#e4e5e7;
   --shadow:0 1px 2px rgba(16,18,22,.06),0 4px 12px rgba(16,18,22,.05);
 }
 @media (prefers-color-scheme:dark){:root{
   --bg:#131314; --card:#1a1a19; --ink:#fff; --dim:#c3c2b7; --line:#2c2c2b;
   --done:#0ca30c; --up:#3987e5; --late:#d03b3b; --lateBg:#2a1717; --track:#2c2c2b;
   --shadow:0 1px 2px rgba(0,0,0,.4);
 }}
 *{box-sizing:border-box}
 body{font:15px/1.55 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;
      margin:0 auto;padding:1.1rem 1rem 3rem;background:var(--bg);color:var(--ink);max-width:37rem}
 h1{font-size:1.35rem;letter-spacing:-.01em;margin:0 0 .9rem}
 .panel{background:var(--card);border:1px solid var(--line);border-radius:.85rem;
        box-shadow:var(--shadow);padding:1rem;margin-bottom:.85rem}

 .top{display:flex;gap:1.1rem;align-items:center}
 .donut{flex:0 0 128px}
 svg{display:block;transform:rotate(-90deg)}
 .hub{font:600 1.5rem/1 -apple-system,system-ui;fill:var(--ink)}
 .hubsub{font:400 .58rem/1 -apple-system,system-ui;fill:var(--dim);letter-spacing:.09em}
 .key{flex:1;min-width:0;display:grid;gap:.45rem}
 .key div{display:flex;align-items:center;gap:.5rem;font-size:.88rem}
 .key i{width:.6rem;height:.6rem;border-radius:.15rem;flex:none}
 .key b{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}
 .key s{color:var(--dim);text-decoration:none;font-size:.8rem;
        font-variant-numeric:tabular-nums;min-width:2.4rem;text-align:right}

 .score{display:flex;align-items:baseline;gap:.55rem;margin-bottom:.15rem}
 .score .n{font-size:1.9rem;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
 .score .r{font-weight:600}
 .score .p{color:var(--dim);font-size:.85rem;margin-left:auto}
 .bar{height:.45rem;border-radius:.3rem;background:var(--track);overflow:hidden;margin:.6rem 0 .35rem}
 .bar i{display:block;height:100%;background:var(--up);border-radius:.3rem}
 .foot{color:var(--dim);font-size:.8rem}

 h2{font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);
    margin:1.4rem .2rem .5rem;display:flex;gap:.45rem;align-items:center}
 h2.late{color:var(--late)}
 h2 em{font-style:normal;background:var(--late);color:#fff;border-radius:1rem;
       padding:.05rem .42rem;font-size:.72rem}
 .row{background:var(--card);border:1px solid var(--line);border-radius:.7rem;
      padding:.7rem .85rem;margin-bottom:.4rem;display:flex;gap:.7rem;align-items:center}
 .row.l{border-left:3px solid var(--late)}
 .row.u{border-left:3px solid var(--up)}
 .row.off{opacity:.55}
 .t{flex:1;min-width:0}
 .t b{font-weight:550;display:block;overflow-wrap:anywhere;font-size:.93rem}
 .m{font-size:.78rem;color:var(--dim);margin-top:.1rem}
 .m .c{font-weight:600;color:var(--ink)}
 .m .c.l{color:var(--late)} .m .c.u{color:var(--up)}
 .pts{font-size:.76rem;font-weight:600;font-variant-numeric:tabular-nums;
      color:var(--done);white-space:nowrap}
 .pts.neg{color:var(--late)}
 .pts.zero{color:var(--dim);font-weight:400}
 button{padding:.44rem .8rem;font:inherit;font-size:.82rem;font-weight:550;
        border-radius:.45rem;border:1px solid var(--line);background:var(--bg);
        color:var(--ink);cursor:pointer;white-space:nowrap}
 button:hover{border-color:var(--dim)}
 .row.l button{border-color:var(--late);color:var(--late)}

 details{margin-top:.5rem}
 summary{cursor:pointer;color:var(--dim);font-size:.83rem;padding:.45rem .2rem;
         letter-spacing:.03em;text-transform:uppercase}
 table{width:100%;border-collapse:collapse}
 td{padding:.3rem .25rem;font-size:.9rem}
 td:first-child{width:1.6rem} td:nth-child(2){width:2.9rem;color:var(--dim)}
 input[type=time]{font:inherit;font-size:.85rem;padding:.28rem .4rem;border-radius:.4rem;
                  border:1px solid var(--line);background:var(--bg);color:var(--ink)}
 input[type=time]:disabled{opacity:.35}
 input[type=checkbox]{width:1.05rem;height:1.05rem;accent-color:var(--up)}
 .save{margin-top:.7rem}
 textarea{width:100%;height:9rem;font:12px/1.5 ui-monospace,monospace;padding:.55rem;
          border-radius:.5rem;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
 .err{background:var(--lateBg);border:1px solid var(--late);color:var(--late);
      padding:.6rem .8rem;border-radius:.5rem;font-size:.87rem;margin-bottom:.85rem}
 .clear{text-align:center;padding:2rem 1rem;color:var(--dim)}
 .clear b{display:block;font-size:1.1rem;color:var(--ink);margin-bottom:.3rem}
</style>

<h1>Assignments</h1>
{% if error %}<p class=err>{{ error }}</p>{% endif %}

<div class=panel>
  <div class=top>
    <div class=donut>
      <svg width="128" height="128" viewBox="0 0 128 128" role="img"
           aria-label="{{ done_count }} of {{ total }} assignments done">
        <circle cx="64" cy="64" r="{{ radius }}" fill="none" stroke="var(--track)"
                stroke-width="13"></circle>
        {% for s in segments %}
        <circle cx="64" cy="64" r="{{ radius }}" fill="none" stroke="{{ s.color }}"
                stroke-width="13" stroke-linecap="butt"
                stroke-dasharray="{{ s.dash }}" stroke-dashoffset="{{ s.offset }}"
        ><title>{{ s.label }}: {{ s.count }}</title></circle>
        {% endfor %}
        <text class="hub" x="64" y="62" text-anchor="middle" dominant-baseline="middle"
              transform="rotate(90 64 64)">{{ percent }}%</text>
        <text class="hubsub" x="64" y="80" text-anchor="middle"
              transform="rotate(90 64 64)">DONE</text>
      </svg>
    </div>
    <div class=key>
      {% for s in segments %}
      <div><i style="background:{{ s.color }}"></i>{{ s.label }}<s>{{ s.pct }}%</s><b>{{ s.count }}</b></div>
      {% endfor %}
      <div style="border-top:1px solid var(--line);padding-top:.45rem;color:var(--dim)">
        Total<b>{{ total }}</b></div>
    </div>
  </div>
</div>

<div class=panel>
  <div class=score>
    <span class=n>{{ points }}</span>
    <span class=r>{{ rank }}</span>
    {% if next_rank %}<span class=p>{{ to_next }} to {{ next_rank }}</span>
    {% else %}<span class=p>top rank</span>{% endif %}
  </div>
  <div class=bar><i style="width:{{ rank_pct }}%"></i></div>
  <div class=foot>Early: +{{ 10 }} on time, +2 per day ahead &middot; Late: &minus;5 per day</div>
</div>

{% macro row(i, kind) %}
  <div class="row {{ kind }}">
    <span class=t>
      <b>{{ i.title }}</b>
      <span class=m><span class="c {{ kind }}">{{ i.countdown }}</span> &middot; {{ i.when }}</span>
    </span>
    {% if kind == 'off' %}
      {% if i.backlog %}<span class="pts zero" title="already overdue when tracking started">&mdash;</span>
      {% else %}<span class="pts {{ 'neg' if i.points < 0 }}">{{ '+' if i.points > 0 }}{{ i.points }}</span>{% endif %}
    {% endif %}
    <form method=post action="/t/{{ token }}/{{ 'undone' if kind == 'off' else 'done' }}/{{ i.uid }}">
      <button>{{ 'Undo' if kind == 'off' else 'Done' }}</button>
    </form>
  </div>
{% endmacro %}

{% if overdue %}
  <h2 class=late>Overdue <em>{{ overdue|length }}</em></h2>
  {% for i in overdue %}{{ row(i, 'l') }}{% endfor %}
{% endif %}

{% if soon %}
  <h2>Due soon</h2>
  {% for i in soon %}{{ row(i, 'u') }}{% endfor %}
{% endif %}

{% if not overdue and not soon %}
  <div class="panel clear">
    <b>All clear.</b>
    {% if next_up %}Next up: {{ next_up.title }} &mdash; {{ next_up.countdown }}.
    {% else %}Nothing left this semester.{% endif %}
  </div>
{% endif %}

{% if later %}
<details><summary>Later &middot; {{ later|length }}</summary>
  {% for i in later %}{{ row(i, '') }}{% endfor %}
</details>
{% endif %}

{% if done %}
<details><summary>Done &middot; {{ done|length }}</summary>
  {% for i in done %}{{ row(i, 'off') }}{% endfor %}
</details>
{% endif %}

<details><summary>Work schedule</summary>
<div class=panel>
  <form method=post action="/t/{{ token }}/schedule">
    <table>
      {% for d in days %}
      <tr>
        <td><input type=checkbox id="on_{{ d.day }}" name="on_{{ d.day }}"
                   {{ 'checked' if d.on }}
                   onchange="this.closest('tr').querySelectorAll('input[type=time]')
                             .forEach(t=>t.disabled=!this.checked)"></td>
        <td><label for="on_{{ d.day }}">{{ d.day }}</label></td>
        <td><input type=time name="start_{{ d.day }}" value="{{ d.start }}"
                   {{ '' if d.on else 'disabled' }}>
            &ndash;
            <input type=time name="end_{{ d.day }}" value="{{ d.end }}"
                   {{ '' if d.on else 'disabled' }}>
            {% if d.extra %}<small>+{{ 1 }} more, edit below</small>{% endif %}</td>
      </tr>
      {% endfor %}
    </table>
    <button class=save>Save schedule</button>
  </form>
  <p class=foot>A shift blocks the whole day, so reminders move to your last free
  evening. Overnight shifts are fine &mdash; end before start just means it runs past midnight.</p>
  <details><summary>Advanced &middot; raw JSON</summary>
    <form method=post action="/t/{{ token }}/schedule">
      <textarea name=schedule>{{ raw_schedule }}</textarea>
      <button class=save>Save JSON</button>
    </form>
  </details>
</div>
</details>
"""
