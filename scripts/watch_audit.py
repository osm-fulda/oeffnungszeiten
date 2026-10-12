#!/usr/bin/env python3
"""
watch_audit.py — is each watch actually monitoring opening hours?

"Quiet" is not "working". A watch pointed at a page with no hours on it never fires,
which looks exactly like a well-behaved watch. That mistake hid 77 blind watches out of
360 here until the snapshots were audited (FILTERS.md §0).

This reads what each watch ACTUALLY captured — its latest stored snapshot — and judges it
against the four criteria in FILTERS.md §4:

  1. is it the hours block?      -> any time tokens at all (German-aware)
  2. is it complete?             -> how many distinct weekdays
  3. is it not a constant?       -> identical text shared with other watches (theme boilerplate)
  4. is it stable?               -> how often it changed recently
  5. does an ignore rule eat it? -> a global_ignore_text pattern that discards an hours LINE

Criterion 5 is the one nothing else can see. `ignore_text` is applied to the checksum only, so
the snapshot keeps the ignored lines and a diff of two snapshots still shows them changing —
a pattern widened too far therefore silences a watch while leaving every visible sign intact.

Read-only: it never writes to changedetection.

Examples:
  python3 scripts/watch_audit.py                              # every watch, worst first
  python3 scripts/watch_audit.py --uuid 8f2a1c3d…             # one watch
  python3 scripts/watch_audit.py --html audit.html            # report to open in a browser
  python3 scripts/watch_audit.py --only red                   # just the broken ones
"""
import argparse
import concurrent.futures as cf
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.request

import hours_lang as L
import osm_cd_common as C

RED, AMBER, GREEN = "red", "amber", "green"
RANK = {RED: 0, AMBER: 1, GREEN: 2}
NOISY_CHANGES_30D = 4          # global recheck is 3 days -> ~10 checks/30d
DEFAULT_SETTINGS = "deploy/global-settings.json"
# How far back a watch without hours is searched for a snapshot that had some. Three covers a
# notice that has been rewritten twice, and the search only runs for the few watches whose
# latest capture holds no time at all.
NOTICE_LOOKBACK = 3
# A notice standing in for the hours is a pause, not a blind watch — until it has stood long
# enough to be the page's new normal. Half a year covers a winter break, the longest pause the
# watches here have shown, and matches the recheck interval of no-watch.json.
NOTICE_MAX_DAYS = 183


def ignore_patterns(path=DEFAULT_SETTINGS):
    """The managed global_ignore_text patterns, compiled. Empty list if the file is absent.

    They are stored the way changedetection wants them, `/(?i)…/` — slashes are delimiters, not
    part of the expression, so they have to come off before compiling.
    """
    try:
        with open(path) as fh:
            raw = json.load(fh).get("application", {}).get("global_ignore_text") or []
    except (OSError, ValueError):
        return []
    out = []
    for p in raw:
        body = p.strip()
        if len(body) > 1 and body.startswith("/") and body.endswith("/"):
            body = body[1:-1]
        try:
            out.append((p, re.compile(body)))
        except re.error:
            out.append((p, None))          # reported as broken, not silently dropped
    return out


def ignore_overreach(text, patterns, lang):
    """Lines a global ignore pattern would discard although they carry hours.

    A global ignore is the one setting that can blind a watch **without leaving a trace**: it is
    applied to `text_for_checksuming` only, so the snapshot still shows the line and a diff of two
    snapshots still shows it changing, while changedetection has already discounted it. Widen a
    pattern too far and the watch goes permanently quiet — which reads exactly like "nothing
    changed". Judging that needs the real snapshot LINES: changedetection ignores per line, and
    `captured_sample` is collapsed to one, which turns every neighbouring live-status widget into
    a false alarm.
    """
    hit, surviving = [], 0
    for line in (text or "").splitlines():
        if not (L.time_matches(line) and L.weekdays(line, lang)):
            continue
        killer = next((p for p, rx in patterns if rx and rx.search(line)), None)
        if killer:
            hit.append((killer, line.strip()[:100]))
        else:
            surviving += 1
    return hit, surviving


def snapshot(base_url, key, uuid):
    """(latest snapshot text, history timestamps). Empty text if there is none yet."""
    base = base_url.rstrip('/') + '/api/v1/watch/' + uuid

    def _get(path):
        req = urllib.request.Request(base + path, headers={'x-api-key': key})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode('utf-8', 'replace')
    try:
        hist = json.loads(_get('/history'))
    except Exception:
        return "", []
    if not hist:
        return "", []
    stamps = sorted(hist, key=lambda s: int(s) if str(s).isdigit() else 0)
    try:
        return _get('/history/' + stamps[-1]), stamps
    except Exception:
        return "", stamps


def notice_since(base_url, key, uuid, stamps):
    """When the hours gave way to a notice: the first snapshot after the last one with times.

    None when none of the earlier snapshots held a time either — then the filter never caught
    the hours, which is the blind watch this audit exists to find.
    """
    base = base_url.rstrip('/') + '/api/v1/watch/' + uuid + '/history/'
    after = stamps[-1] if stamps else None
    for stamp in reversed(stamps[-1 - NOTICE_LOOKBACK:-1]):
        try:
            req = urllib.request.Request(base + stamp, headers={'x-api-key': key})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode('utf-8', 'replace')
        except Exception:
            return None
        if L.time_matches(text):
            return after
        after = stamp
    return None


def judge(w, text, stamps, lang, dup_count, patterns=(), since=None, now=None):
    """-> (verdict, [plain-language issues])

    `since` is what notice_since() found: the timestamp from which a notice stands where the
    hours used to be.

    >>> judge({}, "Wir haben Urlaub bis 18.10.", [], "de", 1, since="1790000000",
    ...       now=1790000000 + 86400)[0]
    'amber'
    >>> judge({}, "Wir haben Urlaub bis 18.10.", [], "de", 1, since="1790000000",
    ...       now=1790000000 + 200 * 86400)[0]
    'red'
    >>> judge({}, "Rufen Sie uns an", [], "de", 1)[0]
    'red'
    """
    issues = []
    verdict = GREEN
    filtered = bool([f for f in (w.get("include_filters") or []) if f.strip()])

    if w.get("last_error"):
        return RED, [f"fetch error: {str(w['last_error'])[:120]}"]
    if not text.strip():
        return RED, ["no snapshot yet — the watch has never captured anything"]
    if L.looks_blocked(text):
        return RED, ["the page returns a block/captcha screen, not content"]

    days = L.weekdays_any(text, lang)
    times = L.time_matches(text)

    age = (int(now or time.time()) - int(since)) // 86400 if since else None
    if not times and age is not None and age <= NOTICE_MAX_DAYS:
        # The anchor is right: it held hours until a holiday or a seasonal break replaced them,
        # and when they come back that is a change like any other, so the watch will report it.
        verdict = AMBER
        issues.append(f"a notice has replaced the hours for {age} day(s) (holiday, seasonal "
                      f"break) — the watch reports when they come back")
    elif not times:
        verdict = RED
        issues.append("no opening hours on this page at all — this watch can never fire")
    else:
        if len(days) == 0:
            verdict = AMBER
            issues.append("times found but no weekday named — may not be an hours block")
        elif len(days) < 3:
            verdict = AMBER
            issues.append(f"only {len(days)} weekday(s) captured "
                          f"({', '.join(days)}) — the rest may sit in a sibling element")
        # Uniform hours alone are NOT suspicious — Aldi really does open 08:00-21:00 seven days
        # a week, and flagging that produced 22 false alarms against 2 real findings. It is only
        # evidence of generated boilerplate when the range is the schema.org example default, or
        # when the identical text also shows up on an unrelated watch (reported separately).
        if L.uniform_hours(text, lang) and re.search(r'\b0?9[:.]00\s*-\s*17[:.]00\b', text):
            verdict = AMBER if verdict == GREEN else verdict
            issues.append("every day shows the same 09:00-17:00 — this is the default emitted "
                          "by some themes, so the watch may be monitoring a constant")
        rep = L.repeat_factor(text)
        if rep > 1:
            verdict = AMBER if verdict == GREEN else verdict
            issues.append(f"the same hours are captured {rep}× — the filter matches "
                          f"duplicate copies of the block (desktop + mobile), which can "
                          f"produce a diff every day")

    swallowed, surviving = ignore_overreach(text, patterns, lang)
    if swallowed:
        pat, line = swallowed[0]
        if surviving:
            verdict = AMBER if verdict == GREEN else verdict
            issues.append(f"{len(swallowed)} hours line(s) are discarded by the global ignore "
                          f"pattern {pat} — e.g. {line!r}; {surviving} hours line(s) still "
                          f"count, so the watch is not blind, but that pattern is too wide")
        else:
            verdict = RED
            issues.append(f"EVERY hours line is discarded by the global ignore pattern {pat} "
                          f"(e.g. {line!r}) — this watch can never fire, and its snapshot still "
                          f"shows the hours, so nothing about it looks wrong")

    if dup_count > 1:
        verdict = AMBER if verdict == GREEN else verdict
        issues.append(f"captures text identical to {dup_count - 1} other watch(es) — "
                      f"likely boilerplate, so it monitors a constant")

    if not filtered:
        verdict = AMBER if verdict == GREEN else verdict
        issues.append("no filter — the whole page is watched, so any banner or teaser alerts")

    # Churn is only evidence of CURRENT noise if it is still happening. Fixing a filter
    # does not erase the history it produced, so a plain 30-day count reports watches that
    # were repaired days ago (Subway: 9 changes, all of them before the fix).
    now = time.time()
    epochs = sorted(int(s) for s in stamps if str(s).isdigit())
    recent = [s for s in epochs if s >= now - 30 * 86400]
    if len(recent) >= NOISY_CHANGES_30D:
        last = time.strftime('%Y-%m-%d', time.localtime(epochs[-1]))
        if epochs[-1] >= now - 7 * 86400:
            verdict = AMBER if verdict == GREEN else verdict
            issues.append(f"changed {len(recent)}× in 30 days, most recently {last} — "
                          f"probably noise (rotation, captcha or a live open/closed widget)")
        else:
            issues.append(f"changed {len(recent)}× in 30 days but nothing since {last} — "
                          f"historical noise, looks fixed")

    if w.get("paused"):
        issues.append("paused")
    return verdict, issues


def collect(api, base_url, uuids, names, lang, workers, patterns=()):
    def one(u):
        try:
            w = api.get(u)
        except Exception as e:
            return u, None, "", [], str(e)
        text, stamps = snapshot(base_url, api.key, u)
        if text.strip() and not L.time_matches(text):
            w["_notice_since"] = notice_since(base_url, api.key, u, stamps)
        return u, w, text, stamps, None

    rows = []
    raw = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for u, w, text, stamps, err in ex.map(one, uuids):
            if w is None:
                rows.append({"uuid": u, "name": names.get(u, u[:8]), "url": "",
                             "verdict": RED, "issues": [f"API error: {err}"],
                             "days": [], "chars": 0, "filter": ""})
                continue
            raw[u] = (w, text, stamps)

    dup = {}
    for u, (_w, text, _s) in raw.items():
        if text.strip():
            dup.setdefault(L.fingerprint(text), []).append(u)

    for u, (w, text, stamps) in raw.items():
        fp = L.fingerprint(text) if text.strip() else None
        n_dup = len(dup.get(fp, [])) if fp else 1
        verdict, issues = judge(w, text, stamps, lang, n_dup, patterns,
                                since=w.get("_notice_since"))
        filt = [f for f in (w.get("include_filters") or []) if f.strip()]
        rows.append({
            "uuid": u,
            "name": names.get(u) or w.get("title") or w.get("url", "")[:40],
            "url": w.get("url", ""),
            "verdict": verdict,
            "issues": issues,
            "days": L.weekdays_any(text, lang),
            "chars": len(text.strip()),
            "filter": filt[0] if filt else "",
            "backend": w.get("fetch_backend") or "system",
            "sample": " ".join(text.split())[:160],
        })
    rows.sort(key=lambda r: (RANK[r["verdict"]], -len(r["issues"]), r["name"].lower()))
    return rows


def print_text(rows):
    mark = {RED: "RED  ", AMBER: "AMBER", GREEN: "green"}
    for r in rows:
        days = "".join(d[0] for d in r["days"]) or "-"
        print(f"{mark[r['verdict']]}  {r['name'][:38]:<38} days:{days:<8} {r['chars']:>5}c  {r['url'][:60]}")
        for i in r["issues"]:
            print(f"         - {i}")
    n = {v: sum(1 for r in rows if r["verdict"] == v) for v in (RED, AMBER, GREEN)}
    print(f"\n{len(rows)} watches   RED {n[RED]}   AMBER {n[AMBER]}   green {n[GREEN]}")
    if n[RED]:
        print("RED = broken or blind. Fix with: python3 scripts/filter_wizard.py --uuid <uuid>")


def write_html(rows, path):
    e = html_mod.escape
    n = {v: sum(1 for r in rows if r["verdict"] == v) for v in (RED, AMBER, GREEN)}
    css = """
body{font:14px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:60rem;padding:0 1rem}
h1{font-size:1.4rem} .sum{display:flex;gap:1rem;margin:1rem 0}
.sum div{padding:.5rem 1rem;border-radius:6px;font-weight:600}
.red{background:#fde2e1;color:#7f1d1d}.amber{background:#fdf0d5;color:#78350f}
.green{background:#e3f5e6;color:#14532d}
.w{border-left:5px solid #ccc;padding:.6rem .9rem;margin:.5rem 0;background:#fafafa;border-radius:0 6px 6px 0}
.w.red{border-color:#dc2626}.w.amber{border-color:#d97706}.w.green{border-color:#16a34a}
.n{font-weight:600} .u{color:#555;font-size:.85em;word-break:break-all}
ul{margin:.4rem 0 0;padding-left:1.2rem}.meta{color:#666;font-size:.82em;margin-top:.3rem}
code{background:#eee;padding:.1rem .3rem;border-radius:3px;font-size:.85em}
"""
    parts = [f"<!doctype html><meta charset=utf-8><title>Watch audit</title><style>{css}</style>",
             "<h1>Opening-hours watch audit</h1>",
             f"<div class=sum><div class=red>RED {n[RED]}</div>"
             f"<div class=amber>AMBER {n[AMBER]}</div><div class=green>green {n[GREEN]}</div></div>",
             "<p>RED = broken or watching a page with no opening hours on it. "
             "AMBER = works but is incomplete, noisy or unfiltered. "
             "Fix one with <code>python3 scripts/filter_wizard.py --uuid &lt;uuid&gt;</code>.</p>"]
    for r in rows:
        days = ", ".join(r["days"]) or "none"
        parts.append(f"<div class='w {r['verdict']}'><span class=n>{e(r['name'])}</span>"
                     f"<div class=u>{e(r['url'])}</div>")
        if r["issues"]:
            parts.append("<ul>" + "".join(f"<li>{e(i)}</li>" for i in r["issues"]) + "</ul>")
        parts.append(f"<div class=meta>weekdays: {e(days)} · {r['chars']} chars · "
                     f"backend {e(r.get('backend',''))} · filter: "
                     f"<code>{e(r['filter'] or 'none')}</code></div>")
        if r.get("sample"):
            parts.append(f"<div class=meta>captured: {e(r['sample'])}</div>")
        parts.append("</div>")
    with open(path, "w") as fh:
        fh.write("\n".join(parts) + "\n")
    print(f"wrote {path}  (RED {n[RED]} · AMBER {n[AMBER]} · green {n[GREEN]})")


def main():
    ap = argparse.ArgumentParser(description="Audit what each watch actually captures")
    ap.add_argument("--uuid", action="append", help="audit only this watch (repeatable)")
    ap.add_argument("--lang", default="de")
    ap.add_argument("--only", choices=[RED, AMBER, GREEN], help="show only this verdict")
    ap.add_argument("--html", help="write an HTML report to this path")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--settings", default=DEFAULT_SETTINGS, metavar="FILE",
                    help="managed global settings, checked for ignore patterns that discard "
                         "hours lines (default: %(default)s; skipped if absent)")
    ap.add_argument("--base-url",
                    default=os.environ.get("CD_BASE_URL", "http://localhost:5000"),
                    help="changedetection API root; defaults to $CD_BASE_URL, so the same call "
                         "works in-cluster, on the VPS and through a tunnel "
                         "(see scripts/cd_env.sh)")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--container", default="changedetection")
    args = ap.parse_args()

    api = C.CDIO(args.base_url, C.resolve_api_key(args.api_key, args.container))
    names = {}
    uuids = args.uuid or list((api.list() or {}).keys())
    if not uuids:
        sys.exit("no watches found")
    patterns = ignore_patterns(args.settings)
    broken = [p for p, rx in patterns if rx is None]
    if broken:
        print(f"WARNING: {len(broken)} global_ignore_text pattern(s) are not valid regex and "
              f"ignore nothing: {broken}", file=sys.stderr)
    print(f"auditing {len(uuids)} watches "
          f"({len(patterns)} global ignore pattern(s) considered) …", file=sys.stderr)

    rows = collect(api, args.base_url, uuids, names, args.lang, args.workers, patterns)
    if args.only:
        rows = [r for r in rows if r["verdict"] == args.only]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.html:
        write_html(rows, args.html)
    else:
        print_text(rows)
    return 1 if any(r["verdict"] == RED for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
