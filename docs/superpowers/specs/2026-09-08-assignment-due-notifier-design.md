# Assignment Due Notifier — Design

**Date:** 2026-09-08
**Status:** Approved for planning

## Purpose

Khellon misses Blackboard assignment deadlines because he does not check his
calendar. This system pushes escalating notifications to his phone until he
confirms an assignment is finished. It runs unattended on an Oracle Cloud
Always Free VM.

Success means: no deadline passes without at least one notification that
arrived while he was awake and not at work, and the notifications stop the
moment he marks the work done.

## Constraints

- Oracle Cloud Always Free VM: E2.1.Micro, 1 OCPU, 1 GB RAM, Ubuntu 24.04.
- Single user. No multi-tenancy, no accounts, no login form.
- Must survive reboots without manual intervention.
- The work schedule must be editable without SSH or redeployment, because it
  changes every semester.

## Data source

Blackboard publishes an iCalendar feed at:

```
https://edisonohio.blackboard.com/webapps/calendar/calendarFeed/3815adaf6ea845268b8dcc627c422d88/learn.ics
```

Verified against the live feed on 2026-09-08:

- 49 `VEVENT` entries, one per gradable item across three courses.
- Each carries `UID` (stable, e.g.
  `_blackboard.platform.gradebook2.GradableItem-_1436930_1`), `SUMMARY`,
  `DTSTART` and `DTEND` (identical), and an empty `DESCRIPTION`.
- `DTSTART` is timezone-qualified: `DTSTART;TZID=America/New_York:20260914T235900`.
  This is the due datetime. Most items are due at 23:59; a few at ~16:30.
- The feed declares `X-PUBLISHED-TTL:PT4H`.
- **There is no course identifier anywhere in the feed.** Titles collide across
  courses: "Introductions", "Syllabee Activity" and "Topic 01 - Assignment" each
  appear twice. UIDs remain unique, so tracking is unambiguous even though the
  notification text is not.
- Summaries contain RFC 5545 escapes (`Emotion\, Stress\, and Health`). No line
  folding appears in the current feed, but longer titles would be folded.
  Parsing must handle both, so the `icalendar` library is used rather than
  hand-rolled string splitting.

All 49 items are tracked. Quizzes and discussion posts count as assignments.

## Notification channel

**ntfy.sh.** Free, no account, and its notifications carry tappable action
buttons — which is what makes "mark done" a single tap rather than an app
switch.

The VM publishes to a secret topic name (the topic name is the only secret).
The phone subscribes once in the ntfy app.

## Scheduling rules

### Stages

For an assignment due at `due`, exactly one stage is active at any moment.
Stage boundaries partition the timeline, so a stage that was missed (VM down,
assignment appeared late in the feed) is never sent stale — the current stage
is sent instead.

| Stage | Active window | Priority |
|---|---|---|
| `HEADS_UP` | `[heads_up_at, due - 5h)` | 3 (default) |
| `T5H` | `[due - 5h, due - 2h)` | 3 (default) |
| `T2H` | `[due - 2h, due - 1h)` | 4 (high) |
| `T1H` | `[due - 1h, due - 30m)` | 5 (urgent) |
| `REPEAT` | `[due - 30m, forever)` | 5 (urgent) |

`HEADS_UP`, `T5H`, `T2H` and `T1H` fire once each. `REPEAT` fires every 30
minutes indefinitely, including after the deadline has passed, until the
assignment is marked done.

The escalation offsets `[5, 2, 1]` hours and the 30-minute repeat interval are
configuration values, not constants.

### `heads_up_at`

Walk backwards from the day before the due date toward today, and take the
**latest day that is not a work day**. The heads-up fires at 8:00 PM on that
day.

```
for d in (due_date - 1 day), (due_date - 2 days), ... down to today:
    if d is not a work day:
        return d at 20:00
return (due_date - 1 day) at 20:00      # fallback, see below
```

Worked example — "Chapter 2 Quiz", due Thu Sep 10 at 11:59 PM:

- Wed Sep 9 is free → heads-up Wed Sep 9, 8:00 PM.
- Wed Sep 9 is a work day, Tue Sep 8 is free → heads-up Tue Sep 8, 8:00 PM.

A work shift blocks the **entire day**, not just the shift hours. This is a
deliberate choice: the point of the heads-up is to land on an evening with time
to actually do the work.

**Fallback.** If every day from today through the day before the due date is a
work day, the heads-up fires the day before at 8:00 PM anyway rather than being
skipped. Khellon works three days a week, so this should never trigger; it
exists so the system is never silent.

**Degenerate case.** If `heads_up_at >= due - 5h` (only possible for items due
in the early hours of the morning), the `HEADS_UP` window is empty and the
stage is skipped. The escalation chain covers it.

The work schedule never suppresses `T5H`, `T2H`, `T1H` or `REPEAT`. Those are
tied to the deadline itself and are needed regardless of where he is.

### Quiet hours

`REPEAT` does not send between 00:00 and 08:00. Since most items are due at
23:59, the repeats would otherwise run all night. Repeats resume at 08:00 and
continue every 30 minutes until the item is marked done.

Quiet hours apply only to `REPEAT`. The one-shot stages are rare enough and
important enough to send whenever they land.

### Recomputation

Every tick recomputes `heads_up_at` from the current work schedule. Editing the
work schedule therefore re-aims pending heads-ups immediately, with no restart.

## Work schedule

Stored in `config.json`, edited through the web page. Fixed weekly pattern:

```json
{
  "Mon": ["16:00-21:00"],
  "Tue": [],
  "Wed": [],
  "Thu": ["16:00-21:00"],
  "Fri": [],
  "Sat": ["10:00-18:00"],
  "Sun": []
}
```

A day with a non-empty list is a work day. Absent or empty days are free days.

The shift times are recorded and displayed but do not affect scheduling: the
whole-day rule only asks *whether* a day has a shift. They are stored because
Khellon asked to record them, they make the schedule readable when he edits it,
and switching to an hours-aware rule later needs no data migration.

## Marking done

Two paths, both writing the same `done_at` column:

1. **The Done button in the notification.** ntfy sends an `Actions` header:
   `http, Done, <base_url>/t/<token>/done/<uid>, method=POST, clear=true`.
   One tap, notification clears, no app switch.
2. **The web page.** Lists everything upcoming with a Done button per row,
   plus an Undo for mistakes.

Once `done_at` is set, no further notification is ever sent for that UID, at
any stage.

## Web page

A small Flask app served by waitress under systemd, on port 8080.

| Route | Purpose |
|---|---|
| `GET /t/<token>/` | Upcoming assignments with Done buttons; work schedule editor |
| `POST /t/<token>/done/<uid>` | Mark done |
| `POST /t/<token>/undone/<uid>` | Unmark |
| `POST /t/<token>/schedule` | Save the work schedule |

The token is a 32-character random hex string and is the only access control.
It is compared with `hmac.compare_digest`; any mismatch returns 404. Requests
without the token prefix get 404 — the app exposes nothing at `/`.

Served over plain HTTP. The data is assignment titles, and the exposure is one
port on one VM. **Tailscale is the documented upgrade path** if encryption is
wanted later: it removes the open port entirely and needs no domain name.

## Data model

SQLite at `~/due/due.db`.

```sql
CREATE TABLE assignments (
  uid            TEXT PRIMARY KEY,
  title          TEXT NOT NULL,
  due            TEXT NOT NULL,   -- ISO 8601 with offset, America/New_York
  active         INTEGER NOT NULL DEFAULT 1,
  done_at        TEXT,
  last_repeat_at TEXT
);

CREATE TABLE sent (
  uid     TEXT NOT NULL,
  stage   TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY (uid, stage)
);
```

`sent` is what makes the one-shot stages one-shot. `last_repeat_at` paces
`REPEAT`: the first repeat fires as soon as the window opens (`last_repeat_at`
is NULL), and each subsequent one when `now - last_repeat_at >= 28 minutes` —
the two-minute tolerance absorbing five-minute cron jitter so the interval does
not drift out to 35 minutes.

### Feed reconciliation

On each refresh:

- **New UID** → insert, `active = 1`.
- **UID present, `due` unchanged** → update the title if it changed, nothing else.
- **UID present, `due` changed** → update `due`, delete its `sent` rows, clear
  `last_repeat_at`. The schedule re-arms against the new deadline. A `done_at`
  that is already set is left alone.
- **UID missing from the feed** → set `active = 0`. The row is kept for history;
  no further notifications.

## Components

Four files in `~/due/`:

- **`due.py`** — feed fetch and parse, scheduling rules, ntfy sending, SQLite
  access. Entry point `python due.py tick`.
- **`web.py`** — the Flask page. Imports `due.py` for database and config access.
- **`config.json`** — ICS URL, timezone, ntfy topic and server, base URL, web
  token, heads-up hour, quiet hours, repeat interval, escalation offsets, work
  schedule.
- **`test_due.py`** — assertion-based self-check, no test framework.

The tick process runs for about two seconds every five minutes and exits.
Only the web page is resident, at roughly 40 MB. Comfortable on 1 GB.

### Feed caching

The tick re-downloads the .ics only if the cached copy is more than an hour
old, respecting the feed's four-hour TTL without going stale. A failed download
is logged and the cached copy is used; a network blip must not silence the
notifications.

## Deployment

- Python venv at `~/due/.venv`. Dependencies: `icalendar`, `flask`, `waitress`.
- Cron: `*/5 * * * * cd ~/due && .venv/bin/python due.py tick >> tick.log 2>&1`
- systemd unit `due-web.service` running waitress, `Restart=always`,
  `WantedBy=multi-user.target`.
- Firewall: **both** layers must be opened, and this is where Oracle VMs
  usually fail. The Oracle Cloud VCN security list needs an ingress rule for
  TCP 8080, *and* the Ubuntu image ships iptables rules that drop it — so the
  local rule must be added and persisted with `netfilter-persistent`.
- `tick.log` is written by cron and rotated by a `logrotate` drop-in, so a
  1 GB VM does not fill its disk over a semester.

## Testing

`test_due.py`, run with `python test_due.py`. Plain asserts against a fixed
clock and an in-memory database. It covers:

- Stage boundaries, including the exact instants at `due - 5h`, `due - 2h`,
  `due - 1h`, `due - 30m`, and after `due`.
- `heads_up_at` when the day before is free, when it is a work day, and the
  all-work-days fallback.
- The degenerate case where `heads_up_at >= due - 5h`.
- Quiet hours suppressing `REPEAT` and releasing it at 08:00.
- The 28-minute repeat pacing.
- Parsing a fixture containing an escaped comma and a folded line.
- A changed due date clearing `sent` rows and re-arming.
- A done assignment producing no stage at any time.

## Out of scope

Deliberately not built:

- **Course names in notifications** — the feed does not contain them.
- **HTTPS** — Tailscale is the documented upgrade instead.
- **Manually added tasks** not in Blackboard.
- **Snooze**, multiple users, email or SMS fallback, and a mobile app.
