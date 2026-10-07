#!/usr/bin/env python3
"""
validate_entries.py — check entry files before they reach changedetection.

Runs in CI on every pull request, and locally whenever you like. Two levels:

  structural (always)  — schema, required fields, slug matches filename, URL shape,
                         no duplicate slug→URL confusion, filter looks like a filter
  live (--live)        — fetch the page and confirm the filter still captures opening hours

The live level is the one that matters. A watch pointed at a page with no hours is silent
forever and looks perfectly healthy; that failure hid 77 blind watches here before anyone
noticed. Catching it in review is much cheaper than catching it in an audit six weeks later.

Exit code 1 if any entry fails. Warnings do not fail the run.

Examples:
  python3 scripts/validate_entries.py                      # structural, all entries
  python3 scripts/validate_entries.py --live               # also fetch every page (slow)
  python3 scripts/validate_entries.py --live --only a.json b.json   # just the changed ones
"""
import argparse
import glob
import json
import os
import re
import sys
from urllib.parse import urlparse

REQUIRED = ["schema", "name", "url"]
# The id every example in README, CONTRIBUTING, CONCEPT and the issue form shows.
EXAMPLE_OSM_ID = "node/123456789"
KNOWN = {"schema", "name", "url", "filter", "fetch_backend", "sort_text_alphabetically",
         "trigger_text", "subtractive_selectors", "extract_text",
         "text_should_not_be_present", "webdriver_delay", "tag", "lang", "osm_id",
         "note", "captured_sample", "added", "tags"}
BACKENDS = {"system", "html_requests", "html_webdriver"}
FILTER_PREFIX = ("xpath:", "xpath1:", "json:", "jq:", "css:", "//", "#", ".", "/")
# Absolute XPaths break on the next site edit. This exact shape blinded a watch here once and
# was removed in 7caa701; the UI Visual Selector still emits it, so it is worth rejecting.
ABSOLUTE_XPATH = re.compile(r'^xpath1?:/html(/|\[)|^/html(/|\[)')
BRITTLE = re.compile(r'elementor-element-|fl-(?:node|icon|module|rich)-|hype-obj-'
                     r'|[0-9a-f]{8,}|css-[a-z0-9]{5,}')

# A long hex string is page-builder output when it names a class or an id, and a stable
# business key when it names a record: tredy's store finder pins the Fulda shop with
# data-store-id="0193073ab79d7cfbbd6281eed32c6db3", which identifies the branch and survives
# a redesign. Only the attribute tells the two apart, so exempt data-* selectors.
STABLE_DATA_ATTR = re.compile(r'@data-[a-z-]+\s*=')


def check_structure(path, e, slugs):
    errs, warns = [], []
    slug = os.path.splitext(os.path.basename(path))[0]

    # The documentation needs an osm_id to show, and whatever it shows gets copied. Every id
    # that looks plausible IS plausible — OSM ids carry no reserved range — so a copied one
    # produces an entry that passes every other check and links the alert to a stranger's
    # object. The docs therefore all show one id, and that id is refused here.
    if e.get("osm_id") == EXAMPLE_OSM_ID:
        errs.append(f"osm_id {EXAMPLE_OSM_ID} is the placeholder from the documentation — "
                    f"look the business up on openstreetmap.org and take its own id")

    for f in REQUIRED:
        if f not in e:
            errs.append(f"missing required field '{f}'")
    if e.get("schema") != 1:
        errs.append(f"unsupported schema {e.get('schema')!r} (expected 1)")

    unknown = set(e) - KNOWN
    if unknown:
        warns.append(f"unknown field(s): {', '.join(sorted(unknown))}")

    url = e.get("url", "")
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        errs.append(f"url must be http(s): {url!r}")
    if not p.netloc:
        errs.append(f"url has no host: {url!r}")

    if slug in slugs:
        errs.append(f"duplicate slug (also {slugs[slug]})")
    slugs[slug] = path

    be = e.get("fetch_backend")
    if be and be not in BACKENDS:
        errs.append(f"fetch_backend {be!r} not one of {sorted(BACKENDS)}")

    filt = e.get("filter")
    if filt:
        # A bare token is a valid CSS type selector — `address` is an HTML5 element and
        # `app-opening-hours-table` is a custom element, both legitimate filters.
        bare_css = re.match(r'^[a-zA-Z][\w-]*$', filt)
        if not filt.startswith(FILTER_PREFIX) and not bare_css:
            warns.append(f"filter has no recognised prefix: {filt[:40]!r}")
        if ABSOLUTE_XPATH.search(filt):
            errs.append("absolute XPath — breaks on the next site edit; anchor on text, "
                        "a stable class, or an id instead")
        if BRITTLE.search(filt) and not STABLE_DATA_ATTR.search(filt):
            warns.append("selector contains a generated class/id; it will change when the "
                         "site is next edited")
    else:
        warns.append("no filter — the whole page is watched, so any banner will alert")

    if not e.get("captured_sample"):
        # An error, not a warning: CI does not fetch the page, so this string is the only
        # evidence anyone has that the filter grabbed opening hours rather than a news box.
        errs.append("no captured_sample — nothing in the diff shows what the filter captures")
    return errs, warns


def check_live(e, lang_default="de", browser_ws=None):
    """Fetch the page and confirm the filter still yields opening hours.

    An entry asking for `fetch_backend: html_webdriver` says its hours only exist after
    JavaScript runs, so checking it with a plain fetch proves nothing — 40 of 277 entries here
    are in that class and used to pass on a shrug ("may be anti-bot or JS-only"). With a browser
    reachable (`--browser-ws`, or a service container in CI) those are rendered and checked like
    any other; without one they still only warn, because a contributor without Docker should not
    be blocked from proposing one.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import filter_wizard as W
    import hours_lang as L
    import lxml.html

    errs, warns = [], []
    needs_browser = e.get("fetch_backend") == "html_webdriver"
    if needs_browser and browser_ws:
        import cdp_render
        try:
            html = cdp_render.render(e["url"], ws_url=browser_ws)
        except Exception as exc:
            warns.append(f"render failed ({str(exc)[:60]}) — check the sample by eye")
            return errs, warns
    else:
        if needs_browser:
            warns.append("needs a browser (html_webdriver) and none was given — the filter is "
                         "NOT verified; pass --browser-ws to check it")
        try:
            html = W.fetch_plain(e["url"])
        except Exception as exc:
            warns.append(f"could not fetch ({str(exc)[:60]}) — may be anti-bot or JS-only")
            return errs, warns

    lang = e.get("lang", lang_default)
    filt = e.get("filter", "")
    if filt.startswith("json:"):
        warns.append("JSON-LD filter not evaluated offline; check the sample by eye")
        return errs, warns
    if not filt:
        return errs, warns

    xp = filt.split(":", 1)[1] if filt.startswith(("xpath:", "xpath1:")) else None
    if not xp:
        warns.append("only XPath filters are checked offline")
        return errs, warns
    try:
        doc = W.strip_noise(lxml.html.fromstring(html))
        sel = doc.xpath(xp)
    except Exception as exc:
        if filt.startswith("xpath:"):
            # changedetection evaluates `xpath:` as XPath 3.1 (elementpath); lxml only knows 1.0,
            # so a `parse-json(...)` or `for ... return` filter cannot be checked here.
            warns.append("XPath 3 filter, lxml cannot evaluate it — only changedetection checks it")
        else:
            errs.append(f"filter is not valid XPath / did not evaluate: {str(exc)[:70]}")
        return errs, warns
    if not sel:
        if needs_browser and browser_ws:
            # The page was rendered exactly as changedetection will render it, so an empty
            # match is not a "maybe" any more.
            errs.append("filter matched nothing in the RENDERED page — the filter is wrong")
        else:
            warns.append("filter matched nothing in a plain fetch — expected if the page needs "
                         "a browser, otherwise the filter is wrong")
        return errs, warns

    text = L.clean(" ".join(W.txt_of(x) for x in sel))
    if not L.time_matches(text):
        errs.append("filter captures no time at all — this watch could never fire")
    elif not L.weekdays_any(text, lang):
        warns.append("times captured but no weekday named — may not be an hours block")
    return errs, warns


# The absence list is the counterpart to `entries/`: an object appears in exactly one of them,
# either because it is watched or because there is a recorded reason why it is not. Without it
# every pass re-examines the same hopeless cases, and "how much is left" stays unanswerable.
# The reason names the CAUSE, not the symptom. "no hours published" was the old wording and it
# swept three different things into one bucket: a page that really states nothing, a chain whose
# branch link nobody found, and our own discovery landing on a footer link. Only the first is an
# absence; the other two are work, and work must not hide in a file named "not watched".
REASONS = {
    "no-hours-on-page",        # operator page, checked plain AND rendered, states none
    "appointment-only",        # the page says so — a fact about the business, not a gap
    "social-only",             # only presence is a social profile: login wall, rotating counters
    "delivery-platform-only",  # only presence is a delivery microsite — those are DELIVERY
                               # windows and flip with the shop toggle, so a better address
                               # would not help; this stays a property of the business
    "today-only",              # the page shows only "today", never the week
    "site-unreachable",        # DNS failure or 404
    "anti-bot",                # 403 in every fetch mode, from a home line as well
    "datacenter-block",        # 200 from a home line, 403 from the VPS, same user agent
    "always-open",             # hours known and constant — nothing to observe
}
# A business property gets a date; a property of *our* instance gets an event, because time
# does not change it — a datacenter block is the same tomorrow. "nie" is for what cannot move.
RECHECK = re.compile(r'^(20\d\d-\d\d-\d\d|on-relocation|never)$')
OSM_ID = re.compile(r'^(node|way|relation)/\d+$')


def norm_url(u):
    """Compare URLs the way a human would: ignore a trailing slash and case."""
    return (u or "").rstrip("/").lower()


def check_absences(path, watched):
    """Structure of no-watch.json, and that no page is in both lists.

    Keyed by the page, not by the OSM object: this list says "we looked at this address and
    there is nothing to watch there". One page can carry several businesses, and whether the
    map knows them is a different question from whether the page publishes hours.
    """
    errs = []
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception as exc:
        return [f"{path}: not valid JSON — {exc}"]
    if doc.get("schema") != 2:
        errs.append(f"{path}: unsupported schema {doc.get('schema')!r} (expected 2)")
    gesehen = set()
    for r in doc.get("records", []):
        wer = r.get("name") or r.get("url") or "?"
        url = r.get("url") or ""
        if not url.startswith(("http://", "https://")):
            errs.append(f"{path}: {wer}: url {url!r} must start with http:// or https://")
        if r.get("osm_id") and not OSM_ID.match(r["osm_id"]):
            errs.append(f"{path}: {wer}: osm_id {r['osm_id']!r} is not type/id")
        if r.get("reason") not in REASONS:
            errs.append(f"{path}: {wer}: reason {r.get('reason')!r} not one of {sorted(REASONS)}")
        if not re.match(r'^20\d\d-\d\d-\d\d$', r.get("established") or ""):
            errs.append(f"{path}: {wer}: established missing or not a date")
        # A reason nobody can read is not a reason. The note says what IS on the page, and it is
        # written in German: it quotes a German page ("Liefer zeiten", "Termine nur nach
        # Vereinbarung") and is read by the mapper who works on Fulda. Structure and docs are
        # English so the project can be reused; the evidence about one shop is not.
        if len((r.get("note") or "").strip()) < 30:
            errs.append(f"{path}: {wer}: note must say what the page does show, and how it "
                        f"was checked")
        if not RECHECK.match(r.get("recheck") or ""):
            errs.append(f"{path}: {wer}: recheck must be a date, on-relocation or never")
        if norm_url(url) in gesehen:
            errs.append(f"{path}: {wer}: url listed twice")
        gesehen.add(norm_url(url))
        if norm_url(url) in watched:
            errs.append(f"{path}: {wer}: this page has a watch in entries/ — a page belongs "
                        f"in one list or the other")
    return errs


def main():
    ap = argparse.ArgumentParser(description="Validate entry files")
    ap.add_argument("--entries", default="entries")
    ap.add_argument("--live", action="store_true", help="also fetch each page")
    ap.add_argument("--only", nargs="*", help="validate just these files (CI: changed files)")
    ap.add_argument("--absences", default="no-watch.json",
                    help="the list of objects deliberately not watched, and why")
    ap.add_argument("--browser-ws", default=os.environ.get("BROWSER_WS"), metavar="WS_URL",
                    help="browser for html_webdriver entries, e.g. ws://localhost:3000 "
                         "(with --live). Without it those entries are only warned about.")
    ap.add_argument("--browser-wait", type=int, default=60, metavar="SECONDS",
                    help="wait this long for the browser to accept connections (CI service "
                         "containers race with Chrome's startup)")
    args = ap.parse_args()

    browser_ws = None
    if args.live and args.browser_ws:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import cdp_render
        product = cdp_render.wait_for_browser(args.browser_ws, timeout=args.browser_wait)
        if product:
            browser_ws = args.browser_ws
            print(f"browser: {product} at {args.browser_ws}")
        else:
            # Do not fail the run: a missing browser must not turn into a red PR for someone
            # whose entry needs no rendering at all.
            print(f"warn: no browser at {args.browser_ws} — html_webdriver entries stay "
                  f"unverified")

    paths = args.only or sorted(glob.glob(os.path.join(args.entries, "*.json")))
    paths = [p for p in paths if not os.path.basename(p).startswith(".")]
    if not paths:
        print("no entry files to validate")
        return 0

    slugs, failed, warned, geladen = {}, 0, 0, []
    for path in paths:
        try:
            with open(path) as fh:
                e = json.load(fh)
        except Exception as exc:
            print(f"FAIL {path}: not valid JSON — {exc}")
            failed += 1
            continue
        geladen.append(e)
        errs, warns = check_structure(path, e, slugs)
        if args.live and not errs:
            le, lw = check_live(e, browser_ws=browser_ws)
            errs += le
            warns += lw
        for m in errs:
            print(f"FAIL {os.path.basename(path)}: {m}")
        for m in warns:
            print(f"warn {os.path.basename(path)}: {m}")
        failed += 1 if errs else 0
        warned += 1 if warns and not errs else 0

    watched = {norm_url(e.get("url")) for e in geladen if e.get("url")}
    abwesend = check_absences(args.absences, watched)
    for m in abwesend:
        print(f"FAIL {m}")

    print(f"\n{len(paths)} entries · {failed + (1 if abwesend else 0)} failed · "
          f"{warned} with warnings")
    return 1 if failed or abwesend else 0


if __name__ == "__main__":
    sys.exit(main())
