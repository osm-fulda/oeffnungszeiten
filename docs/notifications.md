# Notifications

A change is only useful if somebody hears about it. Delivery goes into a Matrix room shared with
other OSM mappers, through a small relay that runs next to changedetection.

## Why a relay and not Apprise

Apprise speaks Matrix natively (`matrixs://:<token>@matrix.org/%23<alias>:matrix.org?mode=off`) and
that is the obvious way to do this. It does not work against matrix.org: the homeserver delegates
authentication to MAS, so `/login` hands out **short-lived** access tokens paired with a refresh
token. Apprise stores one static string and cannot refresh, so the token eventually stops working
and every send fails with

```
401 {"errcode":"M_UNKNOWN_TOKEN","error":"Token is not active"}
```

Pasting a fresh token buys weeks, not a fix. Other public homeservers are no escape, MAS is
spreading, and a webhook bridge puts a third party between the watch and the room.

`charts/changedetection/files/matrix_relay.py` owns the session instead: it keeps the refresh
token, mints access tokens on demand, retries once on a mid-send `401`, and resolves the room alias
once. It is stdlib-only, has no DNS name and no TLS, and is reachable only inside the namespace.

**MAS refresh tokens are single use.** Every refresh returns a new one, so the state file is
rewritten atomically after each refresh and lives on its own PVC. That volume is the one piece of
state here that is not disposable: losing it means seeding again with the bot's password.

## How a notification travels

```
watch changes → changedetection → json://changedetection-matrix-relay:8099/notify → Matrix room
```

The URL carries no credential, which is why it lives in `deploy/global-settings.json` as
`application.notification_urls` rather than in each watch: one lever for all watches, and nothing
secret in a watch config. A watch that should stay quiet gets `notification_muted`; a watch that
needs its own wording overrides `notification_title` / `notification_body`.

`notification_format` is stored lower-case (`text`, `markdown`, `html`, `htmlcolor`): the
capitalised label from the UI is not what the datastore holds.

## Seeding the session

The relay is deployed but silent until it has a session. Unseeded it answers `503` and picks the
state file up on the next request, so seeding needs no restart, and there is a running pod to copy
into, which a crash-looping relay would not give.

1. Check that [status.matrix.org](https://status.matrix.org) is green. `M_UNKNOWN` on a login is
   neither a rate limit (`M_LIMIT_EXCEEDED`) nor bad credentials (`M_FORBIDDEN`) but usually a
   homeserver incident, and everything else hangs off this session.

2. Mint the session. Needs the bot's password and a TTY; nothing is echoed or logged:

   ```bash
   python3 scripts/matrix_relay_seed.py --out ~/matrix_relay.json
   ```

   Three things it does not ask for, because they are defaults in the script. Each has a flag,
   so nothing here is fixed:

   | | Default | Flag |
   |---|---|---|
   | Homeserver | `https://matrix-client.matrix.org` | `--homeserver` |
   | Room | `#osm-fulda-openinghours:matrix.org` | `--room` |
   | Bot account | `fulda-timelord-bot` | `--user` |

   The room is stored as its alias and resolved to a room id on the relay's first send, which is
   then written back into the state file. Pointing the relay at a different room therefore means
   seeding again, not just editing the alias.

   The seeder logs in with `refresh_token: true` and refuses to write a state file without one:
   that flag is the whole difference to a hand-rolled `/login` call, which yields a token that
   works today and dies later with nothing to renew it from. It prints `device_id`, `user_id` and
   `expires_in_ms`.

   If this fails with `403` / `M_UNSUPPORTED`, password login is closed for the account and the
   device-code grant is the way in (`https://account.matrix.org/oauth2/device`, token endpoint
   `https://account.matrix.org/oauth2/token`, dynamic client registration per MSC2966).

3. Copy it in and send a test message:

   ```bash
   kubectl -n changedetection cp ~/matrix_relay.json \
       "$(kubectl -n changedetection get pod -l app.kubernetes.io/name=changedetection-matrix-relay \
           -o name | cut -d/ -f2):/config/matrix_relay.json"
   kubectl -n changedetection exec deploy/changedetection-matrix-relay -- python3 -c \
       "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8099/health').read())"
   kubectl -n changedetection exec deploy/changedetection-matrix-relay -- python3 -c \
       "import json,urllib.request as u; print(u.urlopen(u.Request(
        'http://127.0.0.1:8099/notify', method='POST',
        headers={'Content-Type': 'application/json'},
        data=json.dumps({'message': 'relay online'}).encode())).read())"
   ```

   Test **through the running relay**, not with a second process. `matrix_relay.py --test` in a
   `kubectl exec` would open its own session on the same state file, and the refresh token it
   spends is single use: the server would keep the dead one and fail silently at the next
   change. `POST /notify` is the same path a notification takes, so a message in the room means
   delivery works end to end. `/health` reports `{"ok": true, "room_id": …}`.

   `--test` remains for a relay that is *not* running, which is how the session is verified
   before it ever reaches the cluster:

   ```bash
   python3 charts/changedetection/files/matrix_relay.py --state ~/matrix_relay.json --test "hi"
   ```

4. Delete the local copy: the tokens in it are live until the session is logged out.

**Arm delivery only once the relay answers `{"ok": true}`.** changedetection does not queue or
retry a notification: while `notification_urls` points at a relay without a session, every POST
comes back 502 and the change is gone, visible only as `last_notification_error` on the watch.
Rolling the relay out and arming the watches are therefore two commits, in that order.

The bot must have **joined** the room; sending into a room it is not in fails with `M_FORBIDDEN`
even though the alias resolves.

## Was eine Nachricht verlangt

Three kinds of message arrive in the room, and only the first one is usually about opening hours.

**1. `Öffnungszeiten geändert: <name>`**: the watched block changed. Read the diff first, because
it decides which of two jobs this is:

- the diff shows different **hours** → update OSM, and set `check_date:opening_hours=YYYY-MM-DD`
  even when you only confirmed what was already there; that date is how the next mapper knows the
  value was verified.
- the diff shows a clock, a counter, a rotating teaser, a cookie line → the hours did not move,
  the filter did. Fix the filter (`filter_wizard.py --uuid <uuid>`), commit the entry, do not mute
  the watch.
- the diff shows a temporary notice (Betriebsurlaub, renovation, "ab Montag neue Zeiten"), leave
  `opening_hours` alone. The regular hours are still the regular hours, and the notice is gone in
  two weeks.
- the message carries a `⟳ Nur umsortiert` or `⚠ Zeichensalat` line → nothing changed at all,
  see [Umsortiert](#umsortiert) and [Zeichensalat](#zeichensalat). The relay says so itself, so
  these are the two cases that need no reading of the diff.

The block under `Hinweise:` is not part of the page. It carries the two links a reader needs
when the diff turns out to be the filter's fault, and it is rendered below the diff, outside the
line cap, so a long change can never truncate it.

**2. `CSS/xPath filter was not present in the page`**: changedetection's own message, sent after
six consecutive misses, so roughly 18 days at a 3-day cadence. Nothing to do in OSM: the site was
rebuilt and the anchor is gone. Re-run the wizard and commit the new filter.

**3. `N Watches brauchen Aufmerksamkeit`**: the weekly `audit_report.py`. These are the watches
that will never tell you anything themselves, and each finding carries its own first move:

| finding | what it means | first move |
|---|---|---|
| `fetch error: 403` | the host refuses us | fetch the URL from a home connection and from the VPS with the same UA. Only the VPS gets 403 → the host blocks datacenter ranges, drop the watch. Both get 403 → the UA is the problem, not the address |
| `fetch error: 404` | page is gone | find the successor page, otherwise drop the watch |
| `fetch error: 5xx` | the site is broken today | wait one cycle before touching anything |
| `no opening hours on this page at all` | blind watch | look for `/kontakt`, `/oeffnungszeiten`, a branch page; drop it if the business publishes none |
| `no weekday named` / `only N weekday(s)` | filter caught part of the block | the rest sits in a sibling: anchor on the common ancestor |
| `every day shows the same 09:00-17:00` | theme default, not this business | find the visible hours instead |
| `the same hours are captured N×` | the anchor is too high | pick the narrower element |
| `discarded by the global ignore pattern` | `global_ignore_text` swallows real lines | narrow the pattern in `deploy/global-settings.json` |
| `captures text identical to N other watch(es)` | two watches on one page | give each business its own key, usually its address ([FILTERS.md](../FILTERS.md) case 12) |
| `connect_over_cdp: … has been closed` | the shared cluster browser was gone mid-fetch, not a problem with the page | survives the recheck → look at the browser pod |
| `429` | the host is rate-limiting, not blocking | over several runs it is the hoster, not this page |
| `no filters were found` | the site was rebuilt, the anchor is gone | `filter_wizard.py --uuid`, then commit the entry |
| `no filter` | whole page is watched | set one |

**Every fetch error is looked at twice.** One blink of the shared browser writes
`connect_over_cdp` into every `html_webdriver` watch that was in flight, and a 429 is gone by the
next fetch, while a 403 stands for weeks. A single audit cannot tell those apart, so the report
rechecks each fetch error and reports only what survives. A recheck that does not come back in
time is reported as found: a line too many beats a silent failure. Each finding carries its
`uuid`, because the moves above end in `--uuid` and the message is where you start.

**A quiet room is not proof.** Three states send nothing at all: a fetch error only sets
`last_error`, an empty filter result is swallowed, and an over-wide `global_ignore_text` stops the
checksum from moving. That is precisely what the weekly report is for: if it says
"nothing to report", it has actually looked.

## The baseline swap after a filter change

Redrawing a filter means the next check compares a new excerpt against a snapshot taken through
the old one. That difference is not a change of hours, and from the diff alone nothing says so.
Two things keep it readable, both in `entries_sync.py`:

- **It happens at once.** A watch whose `url`, filter, `ignore_text`, `extract_text`,
  `sort_text_alphabetically` or `fetch_backend` changed gets a `?recheck=1` right after the
  update, so the difference lands minutes after the pull request instead of up to three days
  later, when nobody connects the two any more.
- **It is announced first.** The sync posts a short note before the recheck, and marks the page
  as expecting one alert. The relay answers that note under the page's last alert — the one that
  made somebody change the filter — and the alert that follows goes into the same thread with a
  line saying what it is.

The relay remembers one root event per page in `/config/matrix_relay_threads.json`, beside the
session and never inside it: re-seeding copies the session file into the pod, and a failed write
here must not endanger the refresh token. Losing the file costs the threading and nothing else,
every message still arrives, flat. A root older than 30 days is not answered any more, because a
thread under a message that has scrolled out of the timeline hides the follow-up instead of
placing it. The expectation itself expires after a week: a recheck that finds no difference
sends nothing at all, and an expectation left standing would put the announcement in front of
the next real change instead.

Only a message that names the page can be threaded: the relay reads the `Webseite:` line the
global `notification_body` already writes. That is deliberate — the body is a global setting, and
any edit to those re-baselines every watch.

## What a message says, and what it does not link

Two rules shape the text, and both were paid for.

**Guidance sits under a `Hinweise:` line, page content above it.** Everything the sender appends
after the diff used to arrive as unmarked diff lines, and `reorder_note` stays silent when the
diff holds a line it cannot account for — so the `⟳ Nur umsortiert` verdict could not fire for a
single one of the 558 watches whose body carries that block. The separator is what tells the two
apart; it also keeps the guidance out of `MAX_LINES`.

**A guidance link is rendered as its label, not as its address.** Matrix' default push rule
`.m.rule.contains_user_name` matches the local part of a user id as a *word* in `content.body`,
and a slash is a word boundary — so `github.com/<name>/oeffnungszeiten` in every alert made every
alert a personal highlight for exactly one person, in a room where no message is addressed to
anybody. Putting the address in the `href` and the words in the body ends that without anyone
having to switch a push rule off. A one-word label (`Webseite:`, `uuid:`) keeps its address
visible: there the address is the information.

**A changed line arrives as a pair.** changedetection marks only the second half: the old text
stands on a line of its own with no marker, the new one behind `(into)`. Apart they read as two
nearly identical lines that say nothing; the relay folds them into `<old> → <new>`, which is also
what makes an invisible difference visible. The partner of an `(into)` is therefore whatever line
precedes it, not a line marked `(changed)`.

## Zeichensalat

A page can ship a character the fetch cannot decode. What lands in the snapshot is U+FFFD, the
replacement character, one per undecodable byte — so a single narrow no-break space (U+202F,
three bytes in UTF-8) becomes three of them. The line then differs from the stored one without
anything having changed, and `ignore_whitespace` does not help: U+202F is whitespace and would
have been ignored, U+FFFD is not.

Measured on `zum-biereck.9gg.de`, one of four watches on that platform: the page writes U+202F
between the number and `pm` and sends `content-type: text/html` with no charset, naming UTF-8
only in a meta tag. Decoded as anything else, `5–11 pm` arrives as `5–11␦␦␦pm`. It flips back
whenever the next fetch decodes correctly, so the same non-change is reported twice.

`decode_note()` labels it rather than trying to suppress it. The verdict is decidable, not a
guess: the pair has to be the same text once the replacement characters and all whitespace are
gone. `5–11 pm` against `5–12␦␦␦pm` fails that test, and so does `Küche` against `K␦␦che` — a
difference hidden behind the mangled bytes is reported as the change it is.

Which half carries the mangled bytes says nothing about the verdict, so both halves are read for
them. The failing fetch reports the good stored line against a mangled new one; the fetch after
it reports the same non-change the other way round, arrow pointing at clean text. That second
direction is the one that reads like a real correction, so it is the one that needs the label.

Suppressing it is not on offer. `ignore_text` works line by line, so muting the artifact would
mute every real change to the same line, which is the opening hours themselves.

## Umsortiert

A page whose opening-hours table starts at **today** rewrites itself daily. Its diff shows every
line twice — once removed, once added — with identical times, which reads exactly like changed
hours:

```
− Sonntag  12:00 - 21:00
− Montag   12:00 - 21:00
+ Montag   12:00 - 21:00
+ Sonntag  12:00 - 21:00
```

The relay recognises that shape and labels it, because the answer is always the same one and
nobody should have to work it out from six identical-looking lines. It never suppresses the
message: a watch that says nothing is a watch nobody checks.

**Switching sorting on produces one of these too**, and the diff cannot tell the two apart: the
first sorted snapshot runs against the last unsorted one, which is a reordering like any other.
That is why the note names both readings. If the entry already carries the flag, the message is
that one alarm and there is nothing to do; `rotation_check.py` confirms it as `SETTLED`.

**What to do**, once:

1. `python3 scripts/rotation_check.py --url <the Webseite line from the message>` — it compares
   the stored snapshots and says `ROTATION` when the sorted text is identical across all of them.
   Fetching the page proves nothing here, because the rotation depends on the time of day.
2. Set `"sort_text_alphabetically": true` in the entry, or let `rotation_check.py --fix` write it.
   The entry is the source; a change made in the UI is undone by the next sync.
3. Commit it. Expect **exactly one** more alarm for that watch: the first sorted snapshot runs
   against the last unsorted one. `rotation_check.py` calls that state `SETTLED`, so a later
   reader can tell it apart from a filter that failed.

Sorting hides re-ordering, **not duplication**. If the alarm returns after that, the same line is
in the capture twice — a "today" widget inside the same wrapper — and the filter has to get
narrower ([FILTERS.md](../FILTERS.md) case 7).

**Without a checkout**, the issue form *Filter korrigieren* does the reading part: paste the
`Webseite:` line and the diff, a maintainer applies the `filter-fix` label, and the bot answers
with what the entry's filter grabs today above the alternatives. A wandering "today" block is
invisible in one capture and plain the moment the two sit under each other. The one case it hands
back is rotation: the bot fetches once and a single fetch cannot show it, so the comment says to
report it rather than pick a narrower filter.

When the page carries no hours at all any more — rebuilt, or down to a contact form — no filter
fixes that, and the form for it is *Watch entfernen*: the page, a reason from a list, and a
sentence on what it shows instead. Label `watch-weg`, and the bot builds the pull request that
deletes the entry and writes the block-list record in one commit, carrying the watch's last
`captured_sample` into the note. It fetches nothing; the reading is the reporter's, the merge is
the check.

## Running this for another city

The structure is language-neutral; the text a mapper reads is not. Six places carry German, and
they are the whole list:

| | Where | What it says |
|---|---|---|
| `DEFAULT_LINK_LABEL` | `charts/changedetection/files/matrix_relay.py` | `Webseite`, the label on the header link when a body line names none |
| `reorder_note()` | `charts/changedetection/files/matrix_relay.py` | the `⟳ Nur umsortiert` verdict and what to do about it |
| `decode_note()` | `charts/changedetection/files/matrix_relay.py` | the `⚠ Zeichensalat` verdict, see [Zeichensalat](#zeichensalat) |
| `notification_title`, `notification_body` | `deploy/global-settings.json` | the subject of every change alert, and the `Hinweise:` block under the diff |
| `desired()` | `scripts/entries_sync.py` | the per-watch body, `Webseite:` and `OpenStreetMap:` |
| `compose()` | `scripts/audit_report.py` | the weekly report: title, `Zu tun:` lines, `Webseite:`, `uuid:` |

The relay's own parsing is not language-bound: `LINK_LINE` accepts any label up to 30 characters,
so a translated one keeps producing a header link. `TRAILER_MARK` is the one German word the
parser itself looks for, so a translation has to change it in both places at once. The `(added)` /
`(removed)` / `(changed)` / `(into)` markers it strips come from changedetection itself and are
English wherever it runs. `hours_lang.py` is the other half of this and is
already bilingual; a new language goes in there and both the wizard and the audit gain it.

## Operating it

- **The liveness probe is TCP, not `/health`.** Health means the Matrix session works, and a
  homeserver incident is not something a restart fixes: probing it would take the only relay pod
  out of service for the duration of somebody else's outage.
- **One replica, `Recreate`.** Two relays refreshing in parallel spend each other's single-use
  refresh token.
- **Port 8099 is named senders only.** Anything that can post there can write into a room shared
  with other mappers, so unlike the UI the NetworkPolicy does not open it to the namespace: it
  lists changedetection, the audit report, and the sync while it may prune. A sender the policy
  does not name is not refused, it is dropped on the last hop, so the symptom is a timeout in the
  sender's log and silence in the room.
- **Moving the instance:** copy the state PVC and nothing needs re-authenticating.
- **Noise is a filter problem, not a delivery problem.** A watch that alerts on a clock or a
  cookie banner is a filter to fix (`watch_audit.py` finds them), not a reason to mute delivery.
