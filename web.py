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

# XP. Finish early, gain; finish late, lose.
BASE_XP = 10              # for handing anything in on time
PER_DAY_EARLY = 2         # each full day of head start
EARLY_CAP = 40            # most you can earn from being early
PER_DAY_LATE = 5          # lost per day late
LATE_FLOOR = -30          # worst a single assignment can cost you

# (xp threshold, name, badge colour). Nine tiers over a ~2400 XP semester
# ceiling, so Radiant means finishing nearly everything well ahead of time.
RANKS = [
    (0,    "Iron",      "#6e6e73"),
    (75,   "Bronze",    "#a1642a"),
    (175,  "Silver",    "#9aa4ae"),
    (300,  "Gold",      "#e0a10f"),
    (450,  "Platinum",  "#3fbfb0"),
    (625,  "Diamond",   "#5b9cf8"),
    (825,  "Ascendant", "#2fbf71"),
    (1050, "Immortal",  "#d0416b"),
    (1300, "Radiant",   "#e8c96a"),
]

ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX"]

# What each tier's badge is made of. The ladder is meant to be read at a
# glance from across the room, so every rung adds a layer the one below it
# does not have: a spike burst, then a ribbon, then a laurel wreath, then a
# crown -- and the centre art graduates from a closed book to an open one to
# a mortarboard.  (burst points, centre icon, ribbon, wreath, crown)
ART = [
    (0,  "book", 0, 0, 0),   # Iron      round disc, nothing else
    (0,  "book", 0, 0, 0),   # Bronze    hex plate
    (6,  "book", 0, 0, 0),   # Silver    + 6-point burst
    (8,  "open", 1, 0, 0),   # Gold      + 8-point burst, ribbon
    (12, "open", 1, 0, 0),   # Platinum  + 12-point burst
    (12, "open", 1, 1, 0),   # Diamond   + laurel wreath
    (16, "open", 1, 1, 1),   # Ascendant + crown
    (16, "cap",  1, 1, 1),   # Immortal  + mortarboard
    (20, "cap",  1, 1, 1),   # Radiant   + 20-point burst and halo
]


def _pts(n, ro, ri=None, cx=32, cy=30, rot=-90):
    """Points for an n-gon (ri=None) or an n-pointed star, as an SVG string."""
    count = n if ri is None else n * 2
    out = []
    for k in range(count):
        r = ro if (ri is None or k % 2 == 0) else ri
        a = math.radians(rot + 360 * k / count)
        out.append(f"{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}")
    return " ".join(out)


def _mix(hex_c, other, t):
    """Blend a #rrggbb toward another, for the metal highlight and shadow."""
    c = [int(hex_c[i:i + 2], 16) for i in (1, 3, 5)]
    o = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(a + (b - a) * t) for a, b in zip(c, o))


def badge_art():
    """Everything the badge macro needs to draw all nine tiers."""
    out = []
    for i, (_, name, color) in enumerate(RANKS):
        burst, icon, ribbon, wreath, crown = ART[i]
        out.append({
            "tier": i, "name": name, "color": color,
            "lite": _mix(color, "#ffffff", .55), "dark": _mix(color, "#000000", .42),
            "disc": i == 0, "plate": _pts(6, 20),
            "burst": _pts(burst, 29, 19) if burst else None,
            "icon": icon, "ribbon": bool(ribbon), "wreath": bool(wreath),
            "crown": bool(crown), "halo": i == len(RANKS) - 1, "roman": ROMAN[i],
        })
    return out


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


def xp_for(due_dt, done_at, first_seen=None):
    """XP for one assignment. Nothing is scored until it is done.

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
        return BASE_XP + min(int(days) * PER_DAY_EARLY, EARLY_CAP)
    return max(-PER_DAY_LATE * math.ceil(-days), LATE_FLOOR)


def rank_for(xp):
    """The tier this XP total sits in, and how far it is to the next one."""
    earned = [r for r in RANKS if xp >= r[0]]
    floor, name, color = earned[-1] if earned else RANKS[0]
    tier = RANKS.index((floor, name, color))
    # Tiers above the CURRENT floor, not above the score: a negative total sits
    # below the first threshold, and comparing against it gives a zero-width span.
    later = [r for r in RANKS if r[0] > floor]
    rank = {"name": name, "color": color, "tier": tier,
            "chevrons": 1 + tier // 3}
    if not later:
        rank.update(next_name=None, next_color=None, to_next=0, pct=100)
        return rank
    ceiling, next_name, next_color = later[0]
    rank.update(next_name=next_name, next_color=next_color, to_next=ceiling - xp,
                pct=max(0, round(100 * (xp - floor) / (ceiling - floor))))
    return rank


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
    xp = 0
    for r in db.execute("SELECT * FROM assignments WHERE active=1 ORDER BY due"):
        at = datetime.fromisoformat(r["due"])
        finished = datetime.fromisoformat(r["done_at"]) if r["done_at"] else None
        seen = datetime.fromisoformat(r["first_seen"]) if r["first_seen"] else None
        backlog = seen is not None and at < seen
        scored = xp_for(at, finished, seen)
        xp += scored
        item = {
            "uid": r["uid"], "title": r["title"],
            "when": due.format_when(at),
            "countdown": countdown(at, now),
            "urgent": at - now < timedelta(days=1),
            "xp": scored, "backlog": backlog,
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
    rank = rank_for(xp)
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
        xp=xp, rank=rank, ranks=RANKS, art=badge_art(),
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
<meta charset="utf-8">
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
 .donut svg{display:block;transform:rotate(-90deg)}
 .hub{font:600 1.5rem/1 -apple-system,system-ui;fill:var(--ink)}
 .hubsub{font:400 .58rem/1 -apple-system,system-ui;fill:var(--dim);letter-spacing:.09em}
 .key{flex:1;min-width:0;display:grid;gap:.45rem}
 .key div{display:flex;align-items:center;gap:.5rem;font-size:.88rem}
 .key i{width:.6rem;height:.6rem;border-radius:.15rem;flex:none}
 .key b{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}
 .key s{color:var(--dim);text-decoration:none;font-size:.8rem;
        font-variant-numeric:tabular-nums;min-width:2.4rem;text-align:right}

 .score{display:flex;align-items:center;gap:.85rem;margin-bottom:.15rem}
 .badge{flex:none;display:block;overflow:visible;
        animation:glow var(--d) ease-in-out infinite}
 .badge .shine{fill:#fff;opacity:0;animation:shine var(--s) linear infinite}
 .badge .halo{transform-box:view-box;transform-origin:50% 44.7%;
              stroke-dasharray:3 7;animation:spin var(--r) linear infinite}
 .badge .num{font:700 9px -apple-system,system-ui,sans-serif;fill:#fff;
             letter-spacing:.03em}
 @keyframes glow{0%,100%{filter:drop-shadow(0 0 calc(var(--g) * .3) var(--c))}
                 50%{filter:drop-shadow(0 0 var(--g) var(--c))}}
 @keyframes shine{0%{opacity:0;transform:translateX(0)}
                  10%,32%{opacity:.5}
                  42%,100%{opacity:0;transform:translateX(96px)}}
 @keyframes spin{to{transform:rotate(360deg)}}
 @keyframes hue{to{filter:hue-rotate(360deg)}}
 /* one look per tier: dead metal at the bottom, everything at the top */
 .t0{--g:0px;--d:6s;--s:0s;--r:0s}      .t1{--g:4px;--d:5s;--s:0s;--r:0s}
 .t2{--g:5px;--d:4.4s;--s:6s;--r:0s}    .t3{--g:7px;--d:3.8s;--s:4.5s;--r:0s}
 .t4{--g:8px;--d:3.4s;--s:3.6s;--r:0s}  .t5{--g:9px;--d:3s;--s:2.8s;--r:0s}
 .t6{--g:10px;--d:2.6s;--s:2.6s;--r:14s}.t7{--g:12px;--d:2.2s;--s:2.2s;--r:9s}
 .t8{--g:15px;--d:1.8s;--s:1.7s;--r:6s}
 .t8 .core{animation:hue 5s linear infinite}
 @media (prefers-reduced-motion:reduce){.badge,.badge *{animation:none}}

 .ladder{display:grid;gap:.15rem}
 .lad{display:flex;align-items:center;gap:.7rem;padding:.15rem .35rem;
      border-radius:.5rem;font-size:.88rem}
 .lad.now{background:var(--bg);outline:1px solid var(--line)}
 .lad b{font-weight:600}
 .lad s{margin-left:auto;color:var(--dim);text-decoration:none;font-size:.78rem;
        font-variant-numeric:tabular-nums}
 .who{flex:1;min-width:0}
 .who .rk{display:block;font-size:1.45rem;font-weight:700;letter-spacing:.01em;line-height:1.1}
 .who .xp{display:block;color:var(--dim);font-size:.9rem;font-weight:600;
          font-variant-numeric:tabular-nums;letter-spacing:.02em}
 .nxt{display:flex;align-items:center;gap:.4rem;color:var(--dim);
      font-size:.72rem;line-height:1.25;text-align:right;flex:none}
 .nxt span{font-variant-numeric:tabular-nums}
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

<svg width="0" height="0" aria-hidden="true" style="position:absolute"><defs>
  {% for a in art %}
  <linearGradient id="g{{ a.tier }}" x1="0" y1="0" x2=".3" y2="1">
    <stop offset="0" stop-color="{{ a.lite }}"></stop>
    <stop offset=".42" stop-color="{{ a.color }}"></stop>
    <stop offset="1" stop-color="{{ a.dark }}"></stop>
  </linearGradient>
  <clipPath id="sh{{ a.tier }}">
    {% if a.burst %}<polygon points="{{ a.burst }}"></polygon>{% endif %}
    {% if a.disc %}<circle cx="32" cy="30" r="21"></circle>
    {% else %}<polygon points="{{ a.plate }}"></polygon>{% endif %}
  </clipPath>
  {% endfor %}
</defs></svg>

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

{% macro badge(size, tier) %}
{% set a = art[tier] %}
<svg class="badge t{{ tier }}" width="{{ size }}" height="{{ (size * 1.19)|round|int }}"
     viewBox="0 -4 64 76" style="--c:{{ a.color }}" role="img"
     aria-label="{{ a.name }}, rank {{ tier + 1 }} of {{ art|length }}">

  {% if a.halo %}<circle class="halo" cx="32" cy="30" r="31" fill="none"
       stroke="{{ a.lite }}" stroke-width="1.6" opacity=".85"></circle>{% endif %}

  {% if a.burst %}
  <polygon points="{{ a.burst }}" fill="url(#g{{ tier }})" stroke="{{ a.dark }}"
           stroke-width="1" stroke-linejoin="round" opacity=".92"></polygon>
  {% endif %}

  {% if a.disc %}
  <circle cx="32" cy="30" r="21" fill="url(#g{{ tier }})" stroke="{{ a.dark }}"
          stroke-width="2"></circle>
  <circle cx="32" cy="30" r="17" fill="none" stroke="{{ a.dark }}"
          stroke-width="1" opacity=".7"></circle>
  {% else %}
  <polygon points="{{ a.plate }}" fill="url(#g{{ tier }})" stroke="{{ a.dark }}"
           stroke-width="2" stroke-linejoin="round"></polygon>
  <polygon points="{{ a.plate }}" fill="none" stroke="{{ a.lite }}" stroke-width="1"
           stroke-linejoin="round" opacity=".55"
           transform="translate(32 30) scale(.82) translate(-32 -30)"></polygon>
  {% endif %}

  {% if a.wreath %}
  {% for side in [-1, 1] %}
  <g transform="translate(32 32) scale({{ side }} 1) translate(-32 -32)">
    <path d="M18 55 C8 50 5 40 8 31" fill="none" stroke="{{ a.lite }}"
          stroke-width="2.4" stroke-linecap="round"></path>
    {% for n in range(4) %}
    <ellipse cx="{{ 6.5 + n * 2.6 }}" cy="{{ 51.5 - n * 6.5 }}" rx="4" ry="2.1"
             fill="{{ a.lite }}" stroke="{{ a.dark }}" stroke-width=".7"
             transform="rotate({{ -34 + n * 20 }} {{ 6.5 + n * 2.6 }} {{ 51.5 - n * 6.5 }})"></ellipse>
    {% endfor %}
  </g>
  {% endfor %}
  {% endif %}

  {% if a.crown %}
  <path d="M18 1 L25 10 L32 -3 L39 10 L46 1 L44 16 L20 16 Z"
        fill="url(#g{{ tier }})" stroke="{{ a.dark }}" stroke-width="1.3"
        stroke-linejoin="round"></path>
  <circle cx="32" cy="-1.5" r="2.4" fill="{{ a.lite }}" stroke="{{ a.dark }}"
          stroke-width=".9"></circle>
  {% endif %}

  <g fill="{{ a.dark }}" stroke="{{ a.dark }}" stroke-width="1.4"
     stroke-linejoin="round" stroke-linecap="round">
  {% if a.icon == 'book' %}
    <rect x="22" y="20" width="20" height="16" rx="2" fill="{{ a.dark }}"></rect>
    <rect x="22" y="20" width="6" height="16" rx="2" fill="{{ a.lite }}"
          stroke="none" opacity=".55"></rect>
    <path d="M31 24.5 h7 M31 28 h7 M31 31.5 h4.5" fill="none" stroke="{{ a.lite }}"
          stroke-width="1.5" stroke-linecap="round" opacity=".9"></path>
  {% elif a.icon == 'open' %}
    <path d="M32 25 C29 22 25.5 21 22 21 L22 33 C25.5 33 29 34 32 36.5
             C35 34 38.5 33 42 33 L42 21 C38.5 21 35 22 32 25 Z"
          fill="{{ a.dark }}"></path>
    <path d="M32 25 v11.5" fill="none" stroke="{{ a.lite }}" stroke-width="1.4"></path>
  {% else %}
    <path d="M32 19 L45 25 L32 31 L19 25 Z" fill="{{ a.dark }}"></path>
    <path d="M25.5 27.5 V33 C25.5 35.4 38.5 35.4 38.5 33 V27.5"
          fill="none" stroke="{{ a.dark }}" stroke-width="2"></path>
    <path d="M45 25 V32" fill="none" stroke="{{ a.lite }}" stroke-width="1.3"></path>
    <circle cx="45" cy="33.4" r="1.6" fill="{{ a.lite }}" stroke="none"></circle>
  {% endif %}
  </g>

  {% if a.ribbon %}
  <path d="M9 50 L32 55 L55 50 L52 61 L32 66 L12 61 Z" fill="{{ a.dark }}"
        stroke="{{ a.color }}" stroke-width="1.2" stroke-linejoin="round"
        opacity=".95"></path>
  {% endif %}

  <rect x="24" y="53" width="16" height="13" rx="2.4" fill="#141414"
        stroke="{{ a.color }}" stroke-width="1.4"></rect>
  <text class="num" x="32" y="60" text-anchor="middle"
        dominant-baseline="central">{{ a.roman }}</text>

  <g clip-path="url(#sh{{ tier }})">
    <polygon class="shine" points="-22,-6 -10,-6 -18,78 -30,78"></polygon>
  </g>
</svg>
{% endmacro %}

<div class=panel>
  <div class=score>
    {{ badge(66, rank.tier) }}
    <div class=who>
      <span class="rk" style="color:{{ rank.color }}">{{ rank.name }}</span>
      <span class=xp>{{ xp }} XP</span>
    </div>
    {% if rank.next_name %}
    <div class=nxt>
      {{ badge(32, rank.tier + 1) }}
      <span>{{ rank.to_next }} XP<br>to {{ rank.next_name }}</span>
    </div>
    {% endif %}
  </div>
  <div class=bar><i style="width:{{ rank.pct }}%;background:{{ rank.color }}"></i></div>
  <div class=foot>Early: +10 on time, +2 per day ahead &middot; Late: &minus;5 per day</div>
  <details>
    <summary>All ranks</summary>
    <div class=ladder>
      {% for floor, name, color in ranks %}
      <div class="lad {{ 'now' if loop.index0 == rank.tier }}">
        {{ badge(46, loop.index0) }}
        <b style="color:{{ color }}">{{ name }}</b>
        <s>{{ floor }}{{ '+' if loop.last else '–' ~ (ranks[loop.index][0] - 1) }} XP</s>
      </div>
      {% endfor %}
    </div>
  </details>
</div>

{% macro row(i, kind) %}
  <div class="row {{ kind }}">
    <span class=t>
      <b>{{ i.title }}</b>
      <span class=m><span class="c {{ kind }}">{{ i.countdown }}</span> &middot; {{ i.when }}</span>
    </span>
    {% if kind == 'off' %}
      {% if i.backlog %}<span class="pts zero" title="already overdue when tracking started">&mdash;</span>
      {% else %}<span class="pts {{ 'neg' if i.xp < 0 }}">{{ '+' if i.xp > 0 }}{{ i.xp }} XP</span>{% endif %}
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
