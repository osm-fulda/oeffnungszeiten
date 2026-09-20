#!/usr/bin/env python3
"""
prescreen.py — decide which open objects are worth a filter, before anyone looks at one.

A candidate list lists what is open. Running the wizard over it wastes most of the effort: a batch of five fast-food places produced five absences and no watch, because four of
the domains are microsites that Lieferando runs and states so in their own body text. That is
machine-readable, and so are a dead domain and a page without a single clock value.

So this fetches each page once and sorts it:

    blocked     the host is one this instance cannot fetch — from a home connection it looks
                healthy, in the cluster it is a permanent 403. Read out of no-watch.json
                (`datacenter-block`, `anti-bot`) and blocked-hosts.txt
    platform    the page says a delivery platform runs it — those are DELIVERY windows, and a
                better fetch would not change that
    unreachable DNS failure, refused connection, broken TLS, 4xx/5xx
    throttled   429 or 503 — the host is alive and rate-limiting. Filing that as an absence
                loses a reachable business, so it is its own answer: come back later
    no-times    reachable, and neither the page nor its Kontakt/Impressum/Öffnungszeiten
                subpages carry a weekday with a time. Checking only the front page is not
                enough evidence to record an absence — a third of the hours in this city sit
                on /kontakt
    worth-it    has hours-looking text — this is what the wizard should see

Only the last group needs a human. The first three are already the note that belongs in
`no-watch.json`, error text included.

It also draws **across** categories instead of down a sorted list: sorting by category walks
straight into one industry's platform, and the sample says more about that industry than about
the city.

    python3 scripts/prescreen.py --csv kandidaten.csv --anzahl 10
    python3 scripts/prescreen.py --csv kandidaten.csv --anzahl 10 --kategorie shop

The CSV needs four columns: `osm_id`, `name`, `kategorie`, `website`. Where the list comes from is
not this project's business — a query against OSM, a spreadsheet, a hand-written line.
"""
import argparse
import collections
import csv
import itertools
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import lxml.html

HERE = __file__.rsplit("/", 1)[0]
sys.path.insert(0, HERE)
import osm_cd_common as C  # noqa: E402

# The sentence these microsites carry in their own text. Matched on the body, not on the host,
# because the host is the business's own domain — that is the whole trap.
PLATTFORM = re.compile(r'betrieben und verwaltet durch (lieferando|takeaway|just eat)'
                       r'|powered by (lieferando|takeaway)'
                       r'|diese website wird betrieben und verwaltet', re.I)
ZEIT = re.compile(r'\d{1,2}[:.]\d{2}\s*(?:-|–|—|bis)\s*\d{1,2}[:.]\d{2}'
                  r'|\d{1,2}\s*(?:-|–|bis)\s*\d{1,2}\s*Uhr')
TAG = re.compile(r'\b(mo|di|mi|do|fr|sa|so|montag|dienstag|mittwoch|donnerstag|freitag|samstag'
                 r'|sonntag)', re.I)


def gesperrte_hosts(no_watch="no-watch.json", liste="blocked-hosts.txt"):
    """Hosts that answer here and refuse in the cluster. Two sources, one meaning."""
    raus = set()
    try:
        for r in json.load(open(no_watch, encoding="utf-8"))["records"]:
            if r.get("reason") in ("datacenter-block", "anti-bot") and r.get("url"):
                wirt = urllib.parse.urlsplit(r["url"]).netloc.lower()
                if wirt.startswith("www."):
                    wirt = wirt[4:]
                if wirt:
                    raus.add(wirt)
    except FileNotFoundError:
        pass
    try:
        for zeile in open(liste, encoding="utf-8"):
            zeile = zeile.split("#")[0].strip()
            if zeile:
                raus.add(zeile.lower())
    except FileNotFoundError:
        pass
    return raus


def reihum(rows):
    """One from each category, then the next round — instead of all of one kind first."""
    nach_kat = collections.defaultdict(list)
    for r in rows:
        nach_kat[r["kategorie"]].append(r)
    return [r for r in itertools.chain.from_iterable(
        itertools.zip_longest(*nach_kat.values())) if r]


# Chains put the hours one level down, under the branch list — Fleischerei Gies and Bäckerei
# Happ both read as "no times" without these. Landing there is not the end of the work (a branch
# list needs a keyed row, FILTERS.md case 12), but it is the difference between "nothing to see"
# and "something to do".
UNTERSEITEN = ("kontakt", "kontakt/", "impressum", "oeffnungszeiten", "ueber-uns",
               "filialen", "standorte", "filiale")


def text(url):
    # Through normalize_url like every other fetch here: the candidate list carries the URL as
    # OSM spells it, which is schemeless often enough and non-ASCII on exactly the pages worth
    # screening ('/öffnungszeiten', 'rübsam-metall.de'). Raw, those die before the request.
    req = urllib.request.Request(C.normalize_url(url), headers=C.UA)
    h = urllib.request.urlopen(req, timeout=25).read()
    d = lxml.html.fromstring(h)
    for t in d.xpath("//script|//style"):
        t.getparent().remove(t)
    return " ".join(d.text_content().split())


def zeiten(txt):
    return [m.group(0).strip()
            for m in re.finditer(r'.{0,40}(?:' + ZEIT.pattern + r').{0,40}', txt)
            if TAG.search(m.group(0))]


def pruefe(url):
    try:
        txt = text(url)
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            return "throttled", f"HTTP {e.code} — alive, ask again later"
        return "unreachable", f"HTTPError: {e.code}"
    except Exception as e:
        return "unreachable", f"{type(e).__name__}: {str(e)[:70]}"
    if PLATTFORM.search(txt):
        return "platform", PLATTFORM.search(txt).group(0)
    treffer = zeiten(txt)
    if treffer:
        return "worth-it", " | ".join(dict.fromkeys(treffer))[:200]
    # Nothing on the front page is not yet an answer.
    for suffix in UNTERSEITEN:
        u = urllib.parse.urljoin(url.rstrip("/") + "/", suffix)
        try:
            treffer = zeiten(text(u))
        except Exception:
            continue
        if treffer:
            return "worth-it", f"[{suffix}] " + " | ".join(dict.fromkeys(treffer))[:180]
    return "no-times", "front page and Kontakt/Impressum/Öffnungszeiten checked"


def main():
    ap = argparse.ArgumentParser(description="which open objects are worth a filter")
    ap.add_argument("--csv", required=True,
                    help="candidates: osm_id, name, kategorie, website")
    ap.add_argument("--anzahl", type=int, default=10)
    ap.add_argument("--kategorie", help="only categories starting with this")
    ap.add_argument("--ueberspringen", nargs="*", default=[], metavar="NAME")
    args = ap.parse_args()

    rows = [r for r in csv.DictReader(open(args.csv, encoding="utf-8"))
            if r["name"] not in args.ueberspringen
            and (not args.kategorie or r["kategorie"].startswith(args.kategorie))]
    gesperrt = gesperrte_hosts()
    zaehler = collections.Counter()
    for r in reihum(rows)[:args.anzahl]:
        wirt = urllib.parse.urlsplit(r["website"]).netloc.lower()
        wirt = wirt[4:] if wirt.startswith("www.") else wirt
        if any(wirt == g or wirt.endswith("." + g) for g in gesperrt):
            art, beleg = "blocked", f"{wirt} is unreachable from the cluster"
        else:
            art, beleg = pruefe(r["website"])
        zaehler[art] += 1
        # The osm_id, not the name, is the key: "Sparkasse Fulda" is two objects in this city,
        # and filing a finding against the wrong one buried a branch that does publish its hours.
        print(f"{art:<12} {r['osm_id']:<18} {r['name'][:24]:<24} {beleg[:70]}")
        if art == "worth-it":
            print(f"             {r['website'][:100]}")
    print("\n" + "  ".join(f"{k}: {v}" for k, v in zaehler.most_common()))


if __name__ == "__main__":
    main()
