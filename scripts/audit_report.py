#!/usr/bin/env python3
"""
audit_report.py — post the watches that are actually broken into Matrix.

Three states send no notification of their own, verified in the running image: a fetch error
only sets `last_error` (worker.py), an empty filter result is swallowed because
`empty_pages_are_a_change` is false (processor.py), and an over-wide `global_ignore_text` stops
the checksum from moving at all. A watch in any of them looks exactly like a watch on a page
that simply has not changed.

`watch_audit.py`'s RED verdict covers all three, plus a filter that was blind from the start.
AMBER does not: it is the known and explained material — two watches deliberately sharing a
page, a practice that only publishes two weekdays — which is why this reports RED only and
therefore stays quiet on its own. Nothing is sent when there is nothing to report.

Every fetch error gets a second look before it is reported: the watch is rechecked and audited
again, and only what survives that costs a line. One blink of the shared cluster browser writes
`connect_over_cdp` into every `html_webdriver` watch that was in flight — 8 of 12 findings in one
weekly run — and those are gone by the next fetch, while a 403 that has stood for three weeks is
not. A single audit cannot tell the two apart, and a reader who opens the report days later has
no way to either.

Runs from the sync CronJob's image and reads CD_BASE_URL and CHANGEDETECTION_API_KEY like every
other script here.

    python3 scripts/audit_report.py --dry-run    # print what would be sent
    python3 scripts/audit_report.py --dry-run --no-recheck   # faster, reports the blinks too
    python3 scripts/audit_report.py --relay http://changedetection-matrix-relay:8099/notify
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import osm_cd_common as C     # noqa: E402


def audit(base_url, api_key, uuids=()):
    """Run watch_audit.py and return its rows. Exit 1 there means 'found a RED', not a crash."""
    cmd = [sys.executable, os.path.join(HERE, "watch_audit.py"), "--json"]
    if base_url:
        cmd += ["--base-url", base_url]
    if api_key:
        cmd += ["--api-key", api_key]
    for u in uuids:
        cmd += ["--uuid", u]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if not out.stdout.strip():
        sys.exit(f"watch_audit failed:\n{out.stderr[-800:]}")
    return json.loads(out.stdout)


RECHECK_BUDGET = 420        # seconds to wait for the rechecks to come back
RECHECK_POLL = 15


def confirmed(rows, base_url, api_key, budget=RECHECK_BUDGET):
    """Give every fetch error a second look before it costs a line in the report.

    A fetch error is exactly what this report exists for: nothing else tells anyone about it.
    But not every one of them is standing. The shared cluster browser dropping its connection
    takes out whatever was in flight, so one blink writes `connect_over_cdp` into every
    `html_webdriver` watch at once — measured: 8 of 12 findings in one run — and a 429
    is gone by the next fetch as well. A single audit cannot tell those from a 403 that has
    stood for three weeks, and the reader, who sees the report days later, cannot either.

    So the suspects are rechecked and audited again, and only what survives is reported. A watch
    whose recheck does not come back inside the budget keeps its original row: a line too many
    beats a silent failure. Costs nothing at this size — a run finds a dozen at most.
    """
    suspects = [r for r in rows
                if r.get("verdict") == "red" and r.get("uuid")
                and any("fetch error" in i for i in r.get("issues") or [])]
    if not suspects:
        return rows

    api = C.CDIO(base_url, api_key)
    before, asked = {}, []
    for r in suspects:
        u = r["uuid"]
        try:
            before[u] = (api.get(u) or {}).get("last_checked")
            api.recheck(u)
            asked.append(u)
        except Exception as e:
            print(f"recheck {u} failed: {e}", file=sys.stderr)
    if not asked:
        return rows

    print(f"rechecking {len(asked)} fetch error(s), up to {budget}s …", file=sys.stderr)
    deadline, done = time.time() + budget, set()
    while asked and time.time() < deadline:
        time.sleep(RECHECK_POLL)
        for u in list(asked):
            try:
                if (api.get(u) or {}).get("last_checked") != before[u]:
                    done.add(u)
                    asked.remove(u)
            except Exception:
                pass
    if asked:
        print(f"{len(asked)} recheck(s) did not come back in time — reported as found",
              file=sys.stderr)
    if not done:
        return rows

    # The rechecked rows are replaced whatever their new verdict is, never dropped, so the
    # "von N Watches insgesamt" at the end of the report still counts every watch.
    fresh = {r["uuid"]: r for r in audit(base_url, api_key, sorted(done))}
    out = [fresh.get(r.get("uuid"), r) if r.get("uuid") in done else r for r in rows]
    healed = sum(1 for u in done if fresh.get(u, {}).get("verdict") != "red")
    print(f"{healed} of {len(done)} healed on the second look", file=sys.stderr)
    return out


DOCS = "https://github.com/osm-fulda/oeffnungszeiten/blob/main/docs/repair.md"

# A finding names what is broken; a reader still has to know what to do about it. The audit
# already distinguishes the causes, so each gets its first move here, and the section of
# docs/repair.md that carries the long version: how to tell the causes apart, fix the entry and
# prove the fix. First match wins, so the specific needles stand before the general ones —
# "fetch error: Exception:" alone covers a dead server, an XHTML page and an elementpath assert,
# and each of those has a different fix.
ACTIONS = (
    ("fetch error: Error - 403",
     "Erst die Kennung: antwortet die Seite von der VPS mit aktuellem Chrome-UA mit 200, ist "
     "requests.default_ua veraltet. Sonst von hier und von der VPS vergleichen - nur die VPS "
     "403 heisst Rechenzentrums-Sperre, Watch entfernen.", "403-the-host-refuses"),
    ("HTTP error code 403", None, "403-the-host-refuses"),
    ("fetch error: Error - 404",
     "Die Seite gibt es nicht mehr. Nachfolger suchen, sonst Watch entfernen.",
     "404-the-page-is-gone"),
    ("HTTP error code 404", None, "404-the-page-is-gone"),
    ("fetch error: Error - 5",
     "Serverfehler beim Anbieter. Einen Durchgang abwarten; bleibt es, andere Seite suchen.",
     "5xx-the-server-fails"),
    ("HTTP error code 5", None, "5xx-the-server-fails"),
    ("connect_over_cdp",
     "Der geteilte Browser im Cluster war weg, als der Abruf lief - kein Problem der Seite. "
     "Ueberlebt das den Recheck, ist der Pod selbst dran: kubectl -n changedetection get pods.",
     "connect_over_cdp"),
    ("429",
     "Ratenbegrenzung des Anbieters, keine Sperre. Bleibt sie ueber mehrere Durchgaenge, "
     "trifft sie wahrscheinlich den Hoster und nicht diese eine Seite.", "429-rate-limited"),
    ("no filters were found",
     "Der Filter trifft nichts mehr. Meist wurde die Seite umgebaut, manchmal braucht sie jetzt "
     "JavaScript oder nur lxml trifft: erst validate_entries.py --live, dann "
     "filter_wizard.py --uuid.", "no-filters-were-found"),
    ("Max retries exceeded",
     "Der Server antwortet gar nicht (aus, DNS oder Zertifikat). Von hier und von der VPS "
     "pruefen; bleibt es, Watch entfernen.", "no-answer-max-retries-dns-tls"),
    ("encoding declaration",
     "Die Seite ist XHTML mit <?xml ...?> davor, daran scheitert der einfache Abruf. "
     "fetch_backend: html_webdriver setzen.", "exception-the-parser-fails"),
    ("No parsable JSON",
     "json:-Filter, aber die Seite traegt kein JSON-LD mehr. Neuer Filter: "
     "filter_wizard.py --uuid", "exception-the-parser-fails"),
    ("fetch error: Exception:",
     "Abruf klappt, Auswertung nicht. Steht hinter Exception: nichts, xpath1: statt xpath: "
     "(FILTERS.md Fall 14); sonst den Fehler im Pod nachstellen.",
     "exception-the-parser-fails"),
    ("fetch error",
     "Abruf scheitert. Seite von hier und von der VPS mit gleicher Kennung testen.",
     "403-the-host-refuses"),
    ("no opening hours on this page at all",
     "Die Seite fuehrt keine Zeiten mehr. Bessere Seite suchen (/kontakt, /oeffnungszeiten, "
     "Filialseite), sonst Watch entfernen - blind meldet er nie etwas.",
     "no-hours-in-the-capture"),
    ("times found but no weekday named",
     "Der Filter fasst nur einen Teil des Blocks. Neue Kandidaten: filter_wizard.py --uuid",
     "incomplete-or-duplicated-capture"),
    ("weekday(s) captured",
     "Die restlichen Tage stehen im Nachbarelement. Gemeinsamen Vorfahren waehlen: "
     "filter_wizard.py --uuid", "incomplete-or-duplicated-capture"),
    ("every day shows the same",
     "Das ist die Theme-Vorgabe, nicht die Zeit des Betriebs. Sichtbare Zeiten auf der Seite "
     "suchen.", "incomplete-or-duplicated-capture"),
    ("the same hours are captured",
     "Der Filter sitzt zu hoch und trifft mehrere Kopien. Engeres Element waehlen.",
     "incomplete-or-duplicated-capture"),
    ("discarded by the global ignore",
     "global_ignore_text verschluckt echte Zeilen. Muster in deploy/global-settings.json engen.",
     "no-hours-in-the-capture"),
    ("captures text identical to",
     "Zwei Watches fangen denselben Text - einer haengt am falschen Element. Je Betrieb ein "
     "eigener Anker, etwa auf die Filialadresse (FILTERS.md Fall 12).",
     "incomplete-or-duplicated-capture"),
    ("no filter",
     "Ohne Filter alarmiert jedes Banner. Filter setzen: filter_wizard.py --uuid",
     "incomplete-or-duplicated-capture"),
    ("changed", "Verdaechtig haeufig. Diff ansehen: bewegt sich dort eine Uhr statt der Zeiten?",
     None),
    ("paused", "Watch ist pausiert und prueft nichts.", None),
)

# The label each section gets in the message. The relay shows a guidance link as its label, so
# this is the only part of the address a reader sees.
GUIDE = {
    "403-the-host-refuses": "Anleitung Abruf verweigert",
    "404-the-page-is-gone": "Anleitung Seite weg",
    "5xx-the-server-fails": "Anleitung Serverfehler",
    "no-answer-max-retries-dns-tls": "Anleitung Server antwortet nicht",
    "exception-the-parser-fails": "Anleitung Exception",
    "no-filters-were-found": "Anleitung Filter trifft nichts",
    "no-hours-in-the-capture": "Anleitung keine Zeiten im Ausschnitt",
    "incomplete-or-duplicated-capture": "Anleitung Ausschnitt unvollstaendig",
    "connect_over_cdp": "Anleitung Browser weg",
    "429-rate-limited": "Anleitung Ratenbegrenzung",
}


def match(issues):
    """The (first move, doc section) for a finding, or (None, None) when nothing matches.

    A row without its own text borrows the text of the row with the same section, so a
    finding phrased two ways by changedetection still lands in one group.

    >>> match(["fetch error: Error - Request returned a HTTP error code 503"])[1]
    '5xx-the-server-fails'
    >>> match(["fetch error: Exception: HTTPSConnectionPool(host='x', port=443): "
    ...        "Max retries exceeded with url: /"])[1]
    'no-answer-max-retries-dns-tls'
    >>> match(["fetch error: Exception:"])[1]
    'exception-the-parser-fails'
    >>> match(["something new"])
    (None, None)
    """
    for issue in issues:
        for needle, what, anchor in ACTIONS:
            if needle in issue:
                if what is None:
                    what = next(w for _, w, a in ACTIONS if a == anchor and w)
                return what, anchor
    return None, None


def action_for(issues):
    """The first move for a finding, or None when nothing matches."""
    return match(issues)[0]


def shared_hosts(rows):
    """Hosts with more than one finding: one cause, to be diagnosed once.

    Subdomains count as their site, so four restaurants on one platform read as one line.

    >>> shared_hosts([{"url": "https://a.9gg.de/?hl=de"}, {"url": "https://b.9gg.de/"},
    ...               {"url": "https://www.x.de/"}])
    [('9gg.de', 2)]
    """
    from collections import Counter
    from urllib.parse import urlsplit
    hosts = Counter(".".join((urlsplit(r.get("url") or "").hostname or "").split(".")[-2:])
                    for r in rows)
    return sorted(((h, n) for h, n in hosts.items() if h and n > 1), key=lambda x: (-x[1], x[0]))


def compose(rows):
    """-> (title, body) for the relay, or None when there is nothing worth sending."""
    red = [r for r in rows if r.get("verdict") == "red"]
    if not red:
        return None
    title = (f"{len(red)} Watch braucht Aufmerksamkeit" if len(red) == 1
             else f"{len(red)} Watches brauchen Aufmerksamkeit")
    # Group by what has to be done, so one 403 wave costs one instruction line and not one per
    # watch. Findings without a known move come last, under a heading that says so.
    lines, last = [], object()
    for r in sorted(red, key=lambda r: (action_for(r.get("issues") or []) or "zzz",
                                        r.get("name") or "")):
        what = action_for(r.get("issues") or [])
        if what != last:
            lines.append(f"Zu tun: {what}" if what else
                         "Zu tun: unbekannter Befund - Diff und Filter von Hand ansehen.")
            last = what
        # The relay treats a leading "Label: <url>" line as a header link, so the URL goes last
        # on its own line and the reason above it. Playwright errors arrive as a multi-line call
        # log, which would break that shape into unreadable fragments — collapse them.
        issues = re.sub(r'\s+', ' ', '; '.join(r.get('issues') or [])).strip()
        lines.append(f"(changed) {r.get('name')}: {issues[:160]}")
        lines.append(f"Webseite: {r.get('url')}")
        # Several of the moves above end in "--uuid", and the report is the only place the
        # reader has it: the Matrix message is where they start, not the watch list.
        lines.append(f"uuid: {r.get('uuid')}")
    # Below this line the relay renders guidance, not findings: it stays out of the line cap,
    # and the link is shown as its label instead of as an address nobody reads.
    lines.append("Hinweise:")
    shared = shared_hosts(red)
    if shared:
        lines.append("Gleicher Host, vermutlich eine Ursache - einen ansehen, alle reparieren: "
                     + ", ".join(f"{h} ({n})" for h, n in shared))
    for anchor in dict.fromkeys(match(r.get("issues") or [])[1] for r in red):
        if anchor:
            lines.append(f"{GUIDE[anchor]}: {DOCS}#{anchor}")
    lines.append(f"Wie eine Reparatur ablaeuft: {DOCS}#the-loop")
    lines.append(f"(von {len(rows)} Watches insgesamt)")
    return title, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Report RED watches into Matrix via the relay")
    ap.add_argument("--base-url", default=os.environ.get("CD_BASE_URL", "http://localhost:5000"))
    ap.add_argument("--api-key", default=os.environ.get("CHANGEDETECTION_API_KEY"))
    ap.add_argument("--relay", default=os.environ.get(
        "RELAY_URL", "http://changedetection-matrix-relay:8099/notify"))
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--no-recheck", action="store_true",
                    help="report fetch errors as found, without the second look")
    ap.add_argument("--recheck-budget", type=int, default=RECHECK_BUDGET, metavar="SECONDS",
                    help="how long to wait for the rechecks (default: %(default)s)")
    args = ap.parse_args()

    rows = audit(args.base_url, args.api_key)
    if not args.no_recheck:
        rows = confirmed(rows, args.base_url, C.resolve_api_key(args.api_key),
                         args.recheck_budget)
    message = compose(rows)
    if message is None:
        print(f"{len(rows)} watches, no RED — nothing sent")
        return
    title, body = message

    if args.dry_run:
        print(title)
        print(body)
        return

    payload = json.dumps({"title": title, "message": body}).encode()
    req = urllib.request.Request(args.relay, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"sent: HTTP {r.status} {r.read().decode()[:200]}")


if __name__ == "__main__":
    main()
