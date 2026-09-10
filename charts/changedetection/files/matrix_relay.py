#!/usr/bin/env python3
"""Matrix notification relay for changedetection.

Why this exists: matrix.org delegates authentication to MAS (next-gen auth), so
access tokens are short-lived OAuth tokens. Apprise stores one static string and
cannot refresh, which is why `matrixs://:<token>@matrix.org/...` eventually dies
with M_UNKNOWN_TOKEN. This relay owns the session instead: it keeps the refresh
token, mints access tokens on demand, and posts messages into the room.

changedetection talks to it over plain HTTP inside the namespace:

    notification_urls:  json://matrix-relay:8099/notify

Apprise's json:// posts {"title": ..., "message": ..., ...}; both fields are used.
No token travels that way, so the URL is not a secret and can live in the managed
global settings rather than in each watch.

State file (JSON), default /config/matrix_relay.json:

    {
      "homeserver":    "https://matrix-client.matrix.org",
      "room":          "#osm-fulda-openinghours:matrix.org",
      "refresh_token": "...",         # required; rotated on every refresh
      "access_token":  "...",         # optional seed, refreshed automatically
      "room_id":       "!abc:matrix.org"   # filled in on first resolve
    }

MAS refresh tokens are SINGLE USE: every refresh returns a new one, so the state
file is rewritten atomically after each refresh. Keep it on a persistent volume.
"""
import argparse
import collections
import html as html_mod
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("matrix-relay")
STATE_PATH = os.environ.get("MATRIX_RELAY_STATE", "/config/matrix_relay.json")
# matrix.org rejects an event over 64 KiB outright, so an unusually large diff would not arrive
# at all, the one case where knowing that something changed matters most. Truncate well below
# the limit: the message is a pointer to the watch, not the record of the change.
MAX_BODY = 8000
# A line cap on top of the byte cap, because 8000 bytes is still a wall of text in a room, and
# because MAX_BODY drops the HTML body (truncated markup would be unbalanced), so the longest
# message would be the one that loses its links. Measured over 39 sends: median 1 diff line,
# p90 12, max 24. The worst case on record is a practice holiday notice at 47. 40 leaves every
# real message intact and still bounds what one broken filter can do.
MAX_LINES = 40
# Threads: the room shows one alert per page, and the follow-up that belongs to it — "the filter
# changed, the next alert is the baseline swap" — is only useful next to that alert. So the relay
# remembers the last event it sent per page. Kept beside the session, never inside it: re-seeding
# copies the session file into the pod, and that must not carry stale event ids, nor may a failed
# bookkeeping write endanger the refresh token.
THREADS_PATH = os.environ.get("MATRIX_RELAY_THREADS", "/config/matrix_relay_threads.json")
# A root older than this is not answered any more: a thread under a message nobody can still see
# in the timeline hides the follow-up instead of placing it.
THREAD_TTL = 30 * 24 * 3600
# Enough for every watched page to hold one thread, and a bound on a file nothing else prunes.
THREAD_MAX = 400
# How long an announced baseline swap stays announced. The recheck follows the note within
# minutes, and if it fails the next scheduled check is three days later - but a recheck that
# finds no difference sends nothing at all, and the expectation would otherwise wait for the
# next real change and label it as the swap. A week covers the fallback and expires by itself.
AWAIT_TTL = 7 * 24 * 3600
# Header lines of the notification body: "Webseite: <url>", "OpenStreetMap: <url>", or a bare
# URL (the global body, used by the few entries without an osm_id) which is labelled Webseite.
LINK_LINE = re.compile(r"^(?:([^:]{1,30}):\s*)?(https?://\S+)\s*$")
# What the sender puts under this line is guidance, not page content. Keeping the two apart is
# not cosmetic: everything below it used to arrive as unmarked diff lines, and reorder_note
# refuses to speak when the diff holds a line it cannot account for - so the rotation note could
# never fire for the 558 of 559 watches whose body carries this block.
TRAILER_MARK = "Hinweise:"
# A guidance line that ends in a URL becomes that URL, labelled with the words in front of it.
# Two reasons, and the second one is the real one: ninety characters of link read badly, and
# Matrix' default push rule matches the user's own name as a word in `content.body` - in
# "github.com/<name>/oeffnungszeiten" it is one, so every alert arrived as a personal highlight
# for exactly one person in a room where no message is addressed to anybody. A one-word label
# ("Webseite", "uuid") keeps its address visible, because there the address is the information.
TRAILER_LINK = re.compile(r"^(.{1,80}?)\s*:\s+(https?://\S+)$")
DEFAULT_LINK_LABEL = "Webseite"

# changedetection prefixes each changed line with (added) / (removed) / (changed).
# changedetection reports a changed line as a pair: "(changed) <old>" and "(into) <new>". Read
# apart they are two lines that look almost alike and say nothing; read together they are the
# one sentence the alert is about, so they are folded into "<old> → <new>" below.
DIFF_MARKER = re.compile(r"^\((added|removed|changed|into)\)\s?")
SIGIL = {"added": "+", "removed": "−", "changed": "~", "into": "→"}
COLOR = {"added": "#2e7d32", "removed": "#c62828", "changed": "#ef6c00", "into": "#ef6c00"}

# A page whose hours table starts at TODAY sends every removed line straight back as an added
# one. That diff reads like changed hours and is not one, and the answer is always the same, so
# the message carries it instead of leaving the reader to work it out. The check needs nothing
# but the diff: a day name is part of its line, so two businesses swapping their times still
# differ line by line and never look like a reordering.
REORDER_DOC = ("https://github.com/osm-fulda/oeffnungszeiten/blob/main/docs/"
               "notifications.md#umsortiert")
DECODE_DOC = ("https://github.com/osm-fulda/oeffnungszeiten/blob/main/docs/"
              "notifications.md#zeichensalat")
# What a failed decode leaves behind, one per undecodable byte.
BROKEN_CHAR = "\ufffd"


def page_key(url):
    """The key a page is remembered under: scheme and trailing slash are not identity.

    >>> page_key("https://Example.de/Kontakt/") == page_key("http://example.de/Kontakt")
    True
    >>> page_key("https://example.de/a") == page_key("https://example.de/b")
    False
    >>> page_key(None) is None
    True
    """
    if not url:
        return None
    u = url.strip()
    u = re.sub(r"^https?://", "", u, flags=re.I).rstrip("/")
    host, _, rest = u.partition("/")
    return host.lower() + ("/" + rest if rest else "")


def first_link(message):
    r"""The page a notification is about: the first link line of the body.

    The same lines format_message turns into the header, read once more so the sender does not
    have to name the page a second time in a field of its own.

    >>> first_link("Webseite: https://example.de/kontakt\n(added) Mo 9-17")
    'https://example.de/kontakt'
    >>> first_link("(added) Mo 9-17") is None
    True
    """
    first = (message.splitlines() or [""])[0].strip()
    m = LINK_LINE.match(first)
    return m.group(2) if m else None


def labelled_link(line):
    """-> (label, url) for a guidance line whose whole point is the link, else None.

    The label has to be prose. A single word in front of the colon is a field name, and there
    the address itself is what the reader came for.

    >>> labelled_link("Wann der Filter dran ist: https://example.de/doc#x")
    ('Wann der Filter dran ist', 'https://example.de/doc#x')
    >>> labelled_link("Webseite: https://example.de/") is None
    True
    >>> labelled_link("Zu tun: Zeiten in OSM prüfen.") is None
    True
    """
    m = TRAILER_LINK.match(line.strip())
    if not m:
        return None
    label = m.group(1).strip()
    return (label, m.group(2)) if " " in label else None


def reorder_note(kept):
    """The two lines that explain a reordering, or [] when the diff is a real change.

    True only when every removed line comes back as an added one, unchanged and in the same
    number. Anything else — a changed line, a line the diff did not mark, one more removal than
    addition — is a real change and gets no note.

    >>> reorder_note([("removed", "Mo 9-17"), ("added", "Mo 9-17")])[0].startswith("⟳ Nur")
    True
    >>> reorder_note([("removed", "Mo 9-17"), ("added", "Mo 9-18")])
    []
    >>> reorder_note([("removed", "Mo 9-17"), ("added", "Mo 9-17"), (None, "Küche bis 20:30")])
    []
    >>> reorder_note([])
    []
    """
    gone = collections.Counter(t for kind, t in kept if kind == "removed")
    came = collections.Counter(t for kind, t in kept if kind == "added")
    if not gone or gone != came or len(kept) != sum(gone.values()) + sum(came.values()):
        return []
    n = sum(gone.values())
    # Switching sorting on produces one such diff too — the first sorted snapshot against the
    # last unsorted one — and from the diff alone that is indistinguishable from a page still
    # rotating. So the line names both readings instead of guessing at one.
    return [f"⟳ Nur umsortiert — dieselben {n} Zeilen in anderer Reihenfolge, "
            f"die Zeiten sind unverändert.",
            f"  Zu tun: sort_text_alphabetically im Entry setzen. Steht es schon dort, war das "
            f"der einmalige Alarm nach dem Umstellen.",
            f"  Was dahintersteckt: {REORDER_DOC}"]


def decode_note(pairs):
    """The two lines that explain mangled bytes, or [] when the change is a real one.

    A page that ships a character the fetch could not decode puts U+FFFD where that character
    was. The line then differs from the stored one without anything having changed, and
    `ignore_whitespace` cannot help: the replacement character is not whitespace.

    The mangled half can be either one. A fetch that fails to decode reports the good stored
    line against a mangled new one, and the next fetch that decodes correctly reports the same
    non-change the other way round, so both halves have to be looked at.

    True only when every folded pair is the same text once the replacement characters and all
    whitespace are gone. A pair that hides a real difference behind the mangled bytes fails that
    test and is reported as the change it is.

    >>> decode_note([("Freitag 5–11 pm", "Freitag 5–11\ufffd\ufffd\ufffdpm")])[0][:14]
    '⚠ Zeichensalat'
    >>> decode_note([("Freitag 5–11\ufffd\ufffd\ufffdpm", "Freitag 5–11 pm")])[0][:14]
    '⚠ Zeichensalat'
    >>> decode_note([("Freitag 5–11 pm", "Freitag 5–12\ufffd\ufffd\ufffdpm")])
    []
    >>> decode_note([("Küche bis 22", "K\ufffd\ufffdche bis 22")])
    []
    >>> decode_note([("K\ufffd\ufffdche bis 22", "Küche bis 22")])
    []
    >>> decode_note([("Freitag 5–11 pm", "Freitag 5–11 pm")])   # no mangled bytes at all
    []
    >>> decode_note([])
    []
    """
    if not pairs or not any(BROKEN_CHAR in old + new for old, new in pairs):
        return []
    bare = lambda t: re.sub(r"[\ufffd\s]+", "", t)
    if any(bare(old) != bare(new) for old, new in pairs):
        return []
    return ["⚠ Zeichensalat — alt und neu sind gleich, nur ein Zeichen kam kaputt an.",
            "  Keine geänderte Öffnungszeit, nichts zu tun.",
            f"  Woran das liegt: {DECODE_DOC}"]


def format_message(title, message, lead=None):
    r"""Render one notification as (plain_text, html).

    The body changedetection sends is the watch's notification_body: "Webseite: <url>", an
    "OpenStreetMap: <url>" line where the entry has an osm_id, then {{diff}}. Leading link
    lines become the header; the rest is the diff.

    Blank lines and the page's own indentation are dropped, the rest is kept up to MAX_LINES.
    Measured over 39 sends: median 1 diff line, p90 12, max 24, so the cap never touches a real
    message. MAX_BODY in send() is the second, coarser limit, because matrix.org rejects an
    oversized event outright.

    A diff that only reordered itself is labelled as such (see REORDER_DOC), because the six
    identical-looking lines it produces are the one message nobody can read.

    Everything under a `Hinweise:` line is guidance rather than page content: it goes below the
    diff, outside the line cap, and its links are rendered as their label so that no repository
    URL ends up in the plain body (see TRAILER_LINK).

    `lead` is one line put above everything, used for the alert the relay knows is coming: the
    baseline swap after a filter change. Without it that alert reads like a change of hours.

    >>> plain, html = format_message("T", "Webseite: https://example.de/\n(added) Mo 9-17")
    >>> plain.splitlines()
    ['T', 'Webseite: https://example.de/', '', '+ Mo 9-17']
    >>> plain, html = format_message("T", "(added) Mo 9-17", lead="⤴ erwartet")
    >>> plain.splitlines()[:2]
    ['⤴ erwartet', 'T']
    >>> rot = "(removed) Mo 9-17\n(removed) Di 9-17\n(added) Di 9-17\n(added) Mo 9-17"
    >>> plain, html = format_message("T", rot)
    >>> plain.splitlines()[2]
    '⟳ Nur umsortiert — dieselben 2 Zeilen in anderer Reihenfolge, die Zeiten sind unverändert.'
    >>> "sort_text_alphabetically" in plain.splitlines()[3]
    True
    >>> plain, html = format_message("T", "(removed) Mo 9-17\n(added) Mo 9-18")
    >>> "umsortiert" in plain
    False
    >>> plain, html = format_message("T", "(changed) Fr 17-23\n(into) Fr 17-22")
    >>> plain.splitlines()[-1]
    '~ Fr 17-23 → Fr 17-22'
    >>> body = ("(removed) Mo 9-17\n(added) Mo 9-18\nHinweise:\n"
    ...         "Zu tun: Zeiten in OSM prüfen.\nFilter melden: https://example.de/neu")
    >>> plain, html = format_message("T", body)
    >>> plain.splitlines()[-2:]
    ['Zu tun: Zeiten in OSM prüfen.', 'Filter melden']
    >>> '<a href="https://example.de/neu">Filter melden</a>' in html
    True
    >>> rot = "(removed) Mo 9-17\n(added) Mo 9-17\nHinweise:\nFilter melden: https://example.de/"
    >>> "umsortiert" in format_message("T", rot)[0]      # the trailer no longer hides the note
    True
    >>> lang = "\n".join(f"(added) Zeile {i}" for i in range(50))
    >>> plain, html = format_message("T", lang)
    >>> plain.splitlines()[-1]
    '[…] 10 weitere Zeilen, vollständige Änderung im UI'
    >>> len(plain.splitlines())
    43
    >>> html.count("<br/>") == plain.count(chr(10)) - 1
    True
    """
    lines = message.splitlines()
    head = []
    while lines:
        m = LINK_LINE.match(lines[0].strip())
        if not m:
            break
        lines.pop(0)
        head.append((m.group(1) or DEFAULT_LINK_LABEL, m.group(2)))

    # The guidance block, split off before anything counts diff lines. It is not part of the
    # page, so it neither confuses reorder_note nor eats into MAX_LINES: a long diff must not be
    # able to truncate the sentence that says what to do about it.
    trailer = []
    for i, raw in enumerate(lines):
        if raw.strip() == TRAILER_MARK:
            lines, trailer = lines[:i], [t for t in lines[i + 1:] if t.strip()]
            break

    kept = []
    for raw in lines:
        stripped = raw.strip()
        m = DIFF_MARKER.match(stripped)
        text = DIFF_MARKER.sub("", stripped).strip()
        if not text:                       # blank, with or without a marker
            continue
        kept.append((m.group(1) if m else None, text))

    # Fold the pair onto one line. The old half carries NO marker of its own - measured against a
    # real alert, changedetection marks only the "(into)" side - so the partner is simply the
    # line before it, whatever it is. An "(into)" with nothing in front keeps its own line and
    # its arrow: better a lonely arrow than a silently dropped half.
    folded, pairs = [], []
    for kind, text in kept:
        if kind == "into" and folded:
            was = folded[-1][1]
            folded[-1] = ("changed", f"{was} → {text}")
            pairs.append((was, text))
        else:
            folded.append((kind, text))
    kept = folded

    note = reorder_note(kept) or decode_note(pairs)
    rest = 0
    if len(kept) > MAX_LINES:
        rest = len(kept) - MAX_LINES
        kept = kept[:MAX_LINES]

    def esc(s):
        return html_mod.escape(s, quote=False)

    def as_text(line):
        """A guidance line as it reads without markup: the label stands for its link."""
        lk = labelled_link(line)
        return lk[0] if lk else line

    def as_html(line):
        lk = labelled_link(line)
        if not lk:
            return esc(line)
        label, url = lk
        return f'<a href="{html_mod.escape(url, quote=True)}">{esc(label)}</a>'

    text_parts = ([lead] if lead else []) + ([title] if title else [])
    text_parts += [f"{label}: {url}" for label, url in head]
    text_parts += ([""] + [as_text(n) for n in note] if note else []) + [""]
    text_parts += [f"{SIGIL.get(kind, ' ')} {t}" for kind, t in kept]
    if rest:
        text_parts.append(f"[…] {rest} weitere Zeilen, vollständige Änderung im UI")
    if trailer:
        text_parts += [""] + [as_text(t) for t in trailer]

    html_parts = []
    if lead:
        html_parts.append(f"<b>{esc(lead)}</b>")
    if title:
        html_parts.append(f"<b>{esc(title)}</b>")
    for label, url in head:
        href = html_mod.escape(url, quote=True)
        html_parts.append(f'{esc(label)}: <a href="{href}">{esc(url)}</a>')
    if note:
        html_parts.append("")
        for line in note:
            html_parts.append(f"<b>{esc(line)}</b>" if line.startswith("⟳") else as_html(line))
        html_parts.append("")
    for kind, t in kept:
        body = esc(t)
        if kind == "removed":
            body = f"<del>{body}</del>"
        if kind in COLOR:
            body = f'<font color="{COLOR[kind]}">{body}</font>'
        html_parts.append(body)
    if rest:
        html_parts.append(esc(f"[…] {rest} weitere Zeilen, vollständige Änderung im UI"))
    if trailer:
        html_parts.append("")
        html_parts += [as_html(t) for t in trailer]

    return "\n".join(text_parts), "<br/>".join(html_parts)


class ThreadBook:
    """One remembered event per page, so a follow-up lands under the alert it explains.

    Two things are stored per page: the event that opened the thread, and whether the next alert
    for that page is already accounted for. The second one is what makes the baseline swap after
    a filter change readable — the sync announces it before it happens, the alert then arrives in
    that thread and carries a line saying so, instead of looking like changed hours a few days
    later with nothing next to it.

    Losing the file costs nothing but the threading: every message still arrives, flat.

    >>> import tempfile, os
    >>> b = ThreadBook(os.path.join(tempfile.mkdtemp(), "t.json"))
    >>> b.remember("https://example.de/", "$root")
    >>> b.thread_for("https://example.de/")
    ('$root', '$root')
    >>> b.thread_for("https://other.de/") is None
    True
    >>> b.expect("https://example.de/")
    >>> b.pending("https://example.de/")
    True
    >>> b.followed("https://example.de/", "$reply")     # the awaited alert arrived
    >>> b.pending("https://example.de/")
    False
    >>> b.expect("https://example.de/")                 # an alert that never came
    >>> b.pages[page_key("https://example.de/")]["await"] -= AWAIT_TTL + 1
    >>> b.pending("https://example.de/")
    False
    >>> b.thread_for("https://example.de/")             # replies chain to the newest event
    ('$root', '$reply')
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.pages = None

    def _load(self):
        if self.pages is None:
            try:
                with open(self.path) as fh:
                    self.pages = json.load(fh).get("pages") or {}
            except (OSError, ValueError):
                self.pages = {}
        return self.pages

    def _save(self):
        """Best effort. A thread that is not remembered costs a flat message, nothing more."""
        pages = self.pages
        if len(pages) > THREAD_MAX:
            for k in sorted(pages, key=lambda k: pages[k].get("ts", 0))[:len(pages) - THREAD_MAX]:
                pages.pop(k)
        try:
            d = os.path.dirname(self.path) or "."
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".threads.", suffix=".json")
            with os.fdopen(fd, "w") as fh:
                json.dump({"pages": pages}, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not persist %s (%s) - messages still send, threading is lost",
                        self.path, e)

    def thread_for(self, url):
        """(root, latest) for a page whose thread is still worth answering, else None."""
        key = page_key(url)
        if not key:
            return None
        with self.lock:
            rec = self._load().get(key)
            if not rec or time.time() - rec.get("ts", 0) > THREAD_TTL:
                return None
            return rec["root"], rec.get("latest") or rec["root"]

    def remember(self, url, event_id):
        """This message opened a thread: later follow-ups answer under it."""
        key = page_key(url)
        if not key or not event_id:
            return
        with self.lock:
            self._load()[key] = {"root": event_id, "latest": event_id, "ts": time.time()}
            self._save()

    def followed(self, url, event_id):
        """A reply went into the thread: chain the next one to it, and clear the expectation."""
        key = page_key(url)
        if not key:
            return
        with self.lock:
            rec = self._load().get(key)
            if not rec:
                return
            if event_id:
                rec["latest"] = event_id
            rec.pop("await", None)
            self._save()

    def expect(self, url):
        """The next alert for this page is the announced one and belongs in the thread.

        Timestamped, because the alert may never come: a recheck that finds no difference sends
        nothing, and an expectation that waits forever would label the next real change as the
        announced one.
        """
        key = page_key(url)
        if not key:
            return
        with self.lock:
            rec = self._load().get(key)
            if rec:
                rec["await"] = time.time()
                self._save()

    def pending(self, url):
        key = page_key(url)
        with self.lock:
            rec = self._load().get(key) if key else None
            return bool(rec and time.time() - (rec.get("await") or 0) < AWAIT_TTL)


class MatrixSession:
    """Holds the Matrix session and refreshes it when the server rejects a token."""

    def __init__(self, path):
        self.path = path
        self.state = None
        self.mtime = None
        self.homeserver = "https://matrix-client.matrix.org"
        # Every request runs in its own thread against this one session, and a MAS refresh token
        # is single use: two threads refreshing in parallel would replay the same token, which
        # MAS may read as a compromised session and answer by revoking the whole family. Then
        # only re-seeding with the bot's password gets delivery back.
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- state
    def load(self):
        """Read the state file, late and again whenever it changed underneath us.

        Deliberately not done in __init__: seeding copies the file into the running pod, and a
        relay that exited on an unseeded volume would leave nothing to copy into. Unseeded, it
        serves 503 and picks the file up on the next request without a restart.

        Re-reading on a changed mtime covers the other direction, a re-seed, or anything else
        that wrote a fresh single-use refresh token while this process held the spent one. In
        memory it would look fine until the next refresh, and fail with no visible cause.
        """
        with self.lock:
            try:
                mtime = os.stat(self.path).st_mtime_ns
            except FileNotFoundError:
                if self.state is not None:
                    return self.state          # deleted underneath us; memory still works
                raise
            if self.state is not None and mtime == self.mtime:
                return self.state
            with open(self.path) as fh:
                state = json.load(fh)
            if not state.get("refresh_token"):
                raise RuntimeError(f"{self.path}: refresh_token is required")
            self.homeserver = state.get(
                "homeserver", self.homeserver).rstrip("/")
            self.state, self.mtime = state, mtime
            return self.state

    def save(self):
        """Atomic write - a half-written state file would lose the session.

        fsync before the rename: the newest single-use refresh token is the only way back into
        the session, and a token that reached the page cache but not the disk is gone on a node
        crash. Failures are logged rather than raised, the token is already valid in memory, so
        a request that would otherwise have succeeded should not fail on top of it.
        """
        d = os.path.dirname(self.path) or "."
        try:
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".matrix_relay.", suffix=".json")
            with os.fdopen(fd, "w") as fh:
                json.dump(self.state, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            self.mtime = os.stat(self.path).st_mtime_ns
        except OSError as e:
            log.error("could not persist the session to %s (%s) - it now exists only in memory "
                      "and is lost on restart; re-seed if the relay comes back unauthenticated",
                      self.path, e)

    # ------------------------------------------------------------- requests
    def _call(self, method, path, body=None, token=None, absolute=False):
        url = path if absolute else self.homeserver + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": raw[:200]}

    def refresh(self, stale=None):
        """Exchange the refresh token for a fresh access token (compat endpoint).

        `stale` is the access token whose rejection triggered this. Under the lock the token may
        already have been replaced by whoever got there first, and spending a second refresh
        token for the same expiry is what the single-use rule punishes, so hand back what that
        thread minted instead of asking again.
        """
        with self.lock:
            state = self.load()
            if stale is not None and state.get("access_token") not in (None, stale):
                return state["access_token"]
            status, body = self._call("POST", "/_matrix/client/v3/refresh",
                                      {"refresh_token": state["refresh_token"]})
            if status != 200 or "access_token" not in body:
                raise RuntimeError(f"refresh failed: HTTP {status} {body}")
            state["access_token"] = body["access_token"]
            if body.get("refresh_token"):
                state["refresh_token"] = body["refresh_token"]
            self.save()
            log.info("refreshed access token (expires_in_ms=%s)", body.get("expires_in_ms"))
            return state["access_token"]

    def token(self):
        # Under the lock so that a cold start with several notifications in flight mints one
        # token rather than one per thread; the lock is reentrant, refresh() takes it again.
        with self.lock:
            return self.load().get("access_token") or self.refresh()

    def room_id(self):
        state = self.load()
        if state.get("room_id"):
            return state["room_id"]
        alias = urllib.parse.quote(state["room"], safe="")
        token = self.token()
        status, body = self._call("GET", f"/_matrix/client/v3/directory/room/{alias}",
                                  token=token)
        if status == 401:
            status, body = self._call("GET", f"/_matrix/client/v3/directory/room/{alias}",
                                      token=self.refresh(stale=token))
        if status != 200 or "room_id" not in body:
            raise RuntimeError(f"cannot resolve room: HTTP {status} {body}")
        with self.lock:
            self.state["room_id"] = body["room_id"]
            self.save()
        return body["room_id"]

    def send(self, text, html=None, thread=None):
        """Send one m.text message, refreshing once if the token was rejected.

        `thread` is the (root, latest) pair from the ThreadBook. A client that does not render
        threads shows the message as a reply to `latest`, which is what is_falling_back means.
        """
        if len(text) > MAX_BODY:
            text = text[:MAX_BODY] + "\n[…] gekürzt, vollständige Änderung im UI"
        # Truncating markup would emit unbalanced tags, so an oversized formatted_body is
        # dropped instead, the plain body still carries the change.
        if html and len(html) > MAX_BODY:
            html = None
        content = {"msgtype": "m.text", "body": text}
        if html:
            content.update({"format": "org.matrix.custom.html", "formatted_body": html})
        if thread:
            root, latest = thread
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": root,
                                       "is_falling_back": True,
                                       "m.in_reply_to": {"event_id": latest}}
        room = urllib.parse.quote(self.room_id(), safe="")
        # One transaction ID for both attempts: it is the homeserver's idempotency key, so a
        # retry after a rejected token must not read as a second message. It has to be unique
        # across concurrent sends for the same reason, a clock-derived ID collides between two
        # threads in the same millisecond, and the homeserver then answers the second send with
        # the first one's event_id, silently dropping a notification.
        txn = "cd-" + uuid.uuid4().hex
        for attempt in (1, 2):
            token = self.token()
            status, body = self._call(
                "PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}",
                content, token=token)
            if status == 200:
                return body.get("event_id")
            if status == 401 and attempt == 1:
                log.info("token rejected (%s) - refreshing", body.get("errcode"))
                self.refresh(stale=token)
                continue
            raise RuntimeError(f"send failed: HTTP {status} {body}")


# The line the announced alert carries. It says what the diff below it is, because the diff
# itself cannot: an old capture against a new one looks exactly like changed hours.
BASELINE_LEAD = ("⤴ Erwarteter Wechsel der Grundlage nach der Filteränderung — "
                 "alter Ausschnitt gegen neuen, keine geänderte Öffnungszeit.")


def make_handler(session, book=None):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, payload):
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802 - health check
            if self.path.rstrip("/") in ("/health", ""):
                try:
                    self._reply(200, {"ok": True, "room_id": session.room_id()})
                except Exception as e:
                    self._reply(503, {"ok": False, "error": str(e)[:200]})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {"message": raw.decode("utf8", "replace")}
            title = (payload.get("title") or "").strip()
            message = (payload.get("message") or payload.get("body") or "").strip()
            # The page this is about. changedetection names it in the first body line, so the
            # global notification_body stays as it is — and it must, since any edit to the
            # global settings re-baselines every watch.
            page = payload.get("page") or first_link(message)
            # `thread` asks to answer under the page's last alert: the sync says why a filter
            # changed next to the alert that made somebody change it. `awaited` is the other
            # half, set by the same call: the alert that follows is the baseline swap.
            wants_thread = bool(payload.get("thread"))
            awaited = bool(book and not wants_thread and book.pending(page))
            thread = book.thread_for(page) if book and (wants_thread or awaited) else None
            text, formatted = format_message(title, message,
                                             lead=BASELINE_LEAD if awaited else None)
            if not text.strip():
                self._reply(400, {"error": "empty notification"})
                return
            try:
                event_id = session.send(text, html=formatted, thread=thread)
            except Exception as e:
                log.error("send failed: %s", e)
                self._reply(502, {"error": str(e)[:300]})
                return
            if book and page:
                # A thread is only ever opened by a message that stands on its own. The sync
                # note is one when the page has no alert to answer yet, and then the awaited
                # alert answers the note instead — the pair stays together either way.
                if thread:
                    book.followed(page, event_id)
                else:
                    book.remember(page, event_id)
                if payload.get("expect_baseline"):
                    book.expect(page)
            # Line counts in and out: how verbose real diffs get decides whether a cap is
            # needed at all. Deciding that from measurements, not from one bad example.
            diff_lines = sum(1 for ln in text.splitlines() if ln[:1] in ("+", "−", "~"))
            log.info("sent %s (%d chars, %d diff lines from %d raw)%s",
                     event_id, len(text), diff_lines, len(message.splitlines()),
                     " in thread" if thread else "")
            self._reply(200, {"ok": True, "event_id": event_id})

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--threads", default=THREADS_PATH,
                    help="where the per-page root events are remembered; losing this file "
                         "costs the threading, nothing else")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--test", metavar="TEXT", help="send one message and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    session = MatrixSession(args.state)
    if args.test:
        print("event_id:", session.send(args.test))
        return
    try:
        log.info("relay listening on %s:%d -> room %s", args.host, args.port,
                 session.load().get("room"))
    except Exception as e:
        log.warning("relay listening on %s:%d, but no usable session yet (%s) - "
                    "seed %s; it is picked up without a restart",
                    args.host, args.port, e, args.state)
    ThreadingHTTPServer((args.host, args.port),
                        make_handler(session, ThreadBook(args.threads))).serve_forever()


if __name__ == "__main__":
    main()
