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


DOCS = ("https://github.com/osm-fulda/oeffnungszeiten/blob/main/docs/"
        "notifications.md#was-eine-nachricht-verlangt")

# A finding names what is broken; a reader still has to know what to do about it. The audit
# already distinguishes the causes, so each gets its first move here — the doc link at the end
# carries the long version.
ACTIONS = (
    ("fetch error: Error - 403",
     "Erst die Kennung: antwortet die Seite von der VPS mit aktuellem Chrome-UA mit 200, ist "
     "requests.default_ua veraltet. Sonst von hier und von der VPS vergleichen - nur die VPS "
     "403 heisst Rechenzentrums-Sperre, Watch entfernen."),
    ("fetch error: Error - 404",
     "Die Seite gibt es nicht mehr. Nachfolger suchen, sonst Watch entfernen."),
    ("fetch error: Error - 5",
     "Serverfehler beim Anbieter. Einen Durchgang abwarten; bleibt es, andere Seite suchen."),
    ("connect_over_cdp",
     "Der geteilte Browser im Cluster war weg, als der Abruf lief - kein Problem der Seite. "
     "Ueberlebt das den Recheck, ist der Pod selbst dran: kubectl -n changedetection get pods."),
    ("429",
     "Ratenbegrenzung des Anbieters, keine Sperre. Bleibt sie ueber mehrere Durchgaenge, "
     "trifft sie wahrscheinlich den Hoster und nicht diese eine Seite."),
    ("no filters were found",
     "Der Filter trifft nicht mehr, die Seite wurde umgebaut. Neue Kandidaten: "
     "filter_wizard.py --uuid"),
    ("fetch error: Exception:",
     "Steht hinter Exception: nichts, scheitert nicht der Abruf, sondern der XPath-Parser an "
     "Inhalt hinter </html>. Filter mit xpath1: statt xpath: pruefen (FILTERS.md Fall 14)."),
    ("fetch error",
     "Abruf scheitert. Seite von hier und von der VPS mit gleicher Kennung testen."),
    ("no opening hours on this page at all",
     "Die Seite fuehrt keine Zeiten mehr. Bessere Seite suchen (/kontakt, /oeffnungszeiten, "
     "Filialseite), sonst Watch entfernen - blind meldet er nie etwas."),
    ("times found but no weekday named",
     "Der Filter fasst nur einen Teil des Blocks. Neue Kandidaten: filter_wizard.py --uuid"),
    ("weekday(s) captured",
     "Die restlichen Tage stehen im Nachbarelement. Gemeinsamen Vorfahren waehlen: "
     "filter_wizard.py --uuid"),
    ("every day shows the same",
     "Das ist die Theme-Vorgabe, nicht die Zeit des Betriebs. Sichtbare Zeiten auf der Seite "
     "suchen."),
    ("the same hours are captured",
     "Der Filter sitzt zu hoch und trifft mehrere Kopien. Engeres Element waehlen."),
    ("discarded by the global ignore",
     "global_ignore_text verschluckt echte Zeilen. Muster in deploy/global-settings.json engen."),
    ("captures text identical to",
     "Zwei Watches fangen denselben Text - einer haengt am falschen Element. Je Betrieb ein "
     "eigener Anker, etwa auf die Filialadresse (FILTERS.md Fall 12)."),
    ("no filter",
     "Ohne Filter alarmiert jedes Banner. Filter setzen: filter_wizard.py --uuid"),
    ("changed", "Verdaechtig haeufig. Diff ansehen: bewegt sich dort eine Uhr statt der Zeiten?"),
    ("paused", "Watch ist pausiert und prueft nichts."),
)


def action_for(issues):
    """The first move for a finding, or None when nothing matches."""
    for issue in issues:
        for needle, what in ACTIONS:
            if needle in issue:
                return what
    return None


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
    lines.append(f"Was die Befunde bedeuten: {DOCS}")
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
