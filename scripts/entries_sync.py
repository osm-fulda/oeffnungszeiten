#!/usr/bin/env python3
"""
entries_sync.py — reconcile changedetection against entries/*.json (CONCEPT.md).

The entry files are the source of truth. Add a file -> new watch. Edit a file -> the watch is
updated. Delete a file -> the watch is deleted, but only with `--prune`: deletions are otherwise
derived from entries/.lock.json, and a CronJob throws its checkout away every run, so in the
cluster nothing would ever be removed.

`--prune` carries two safeties, because "no file claims this watch" and "the checkout is broken"
look identical from here. It refuses when no entries loaded at all, and it refuses when more
unclaimed watches turn up than `--max-prune` allows. Either way `--notify` says so in Matrix,
where somebody reads it: a Job log survives three runs.

Git is authoritative: a filter tweaked in the UI is reverted on the next sync. Run
`cd_export.py --split entries` to turn a UI experiment into a commit instead.

Identity is the **slug** (the filename), mapped to a changedetection uuid by entries/.lock.json.
It cannot be the URL alone: a handful of pages back two businesses each, so a URL-keyed sync
could not tell which watch an entry owns.

The lock is a cache, not a requirement. An entry whose lock entry is missing or stale is
ADOPTED — matched to an existing watch by URL, and by name against title where one URL has
several candidates — so a sync from a fresh checkout updates watches instead of duplicating
them. That is what lets the reconcile run from a throwaway CronJob checkout.

Changes nothing without --apply. Deletion is always targeted by uuid, never by tag — a tag
filter once wiped all 73 watches.

Examples:
  python3 scripts/entries_sync.py                     # plan only
  python3 scripts/entries_sync.py --apply
  python3 scripts/entries_sync.py --apply --prune     # also delete watches no entry claims
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import sys
import urllib.request

import osm_cd_common as C

# Entry field -> changedetection field. Everything listed is ENFORCED: if the entry and the
# watch disagree, the watch loses.
OSM_BASE = "https://www.openstreetmap.org"
REPO_BASE = "https://github.com/osm-fulda/oeffnungszeiten"
DOCS_BASE = f"{REPO_BASE}/blob/main/docs"

FIELD_MAP = {
    "url": "url",
    "fetch_backend": "fetch_backend",
    "sort_text_alphabetically": "sort_text_alphabetically",
    "trigger_text": "trigger_text",
    "subtractive_selectors": "subtractive_selectors",
    "extract_text": "extract_text",
    "text_should_not_be_present": "text_should_not_be_present",
    "webdriver_delay": "webdriver_delay",
    "name": "title",
}


def norm_url(u):
    """Compare URLs the way a human would: ignore a trailing slash and case."""
    return (u or "").rstrip("/").lower()


def norm_name(s):
    """Compare names ignoring punctuation and case, so "antonius Café" still matches itself."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def load_entries(d):
    out = {}
    for path in sorted(glob.glob(os.path.join(d, "*.json"))):
        slug = os.path.splitext(os.path.basename(path))[0]
        if slug.startswith("."):
            continue
        with open(path) as fh:
            out[slug] = json.load(fh)
    return out


def resolve_tags(api, names, cache, create=True):
    """Tag names -> uuids in THIS instance, creating any that do not exist yet.

    Tags are separate objects referenced by uuid, so the uuid differs per instance. Entries
    therefore carry names, and each instance maps them to its own uuids.
    """
    out = []
    for name in names:
        if name not in cache:
            if not create:
                continue          # plan mode: report against what exists, invent nothing
            cache[name] = api.tag_create(name)
            print(f"created tag {name} -> {str(cache[name])[:8]}")
        if cache.get(name):
            out.append(cache[name])
    return sorted(out)


def adoptions(entries, live, lock):
    """slug -> uuid for every entry whose lock entry is missing or stale.

    An entry that adopts nothing is created, so this is the whole defence against a sync from a
    fresh checkout duplicating every watch. That is not hypothetical: the CronJob discards its
    checkout, so the lock it writes is gone by the next run, and a lock carried over from a
    different instance matches nothing at all. In the cluster this function decides identity.

    Two passes, and the order is the point. The URL is the better key and goes first; it is not
    unique, though (a handful of pages back two businesses each), so a URL with several
    candidates is decided by name against title.

    >>> live = {"u1": {"url": "https://a.de/", "title": "A"},
    ...         "u2": {"url": "https://b.de/", "title": "B"}}
    >>> adoptions({"a": {"url": "https://a.de", "name": "A"}}, live, {}) == {"a": "u1"}
    True

    The second pass exists for the one move the URL cannot follow: the entry's URL changed. To a
    URL match that reads as two unrelated facts, an entry pointing at nothing and a watch nobody
    claims, and the answer would be to delete the watch and build a new one, losing its history
    and its snapshots. A watch whose title is exactly this entry's name, that no URL claims and
    no other entry wants, is the same watch at a new address.

    >>> adoptions({"a": {"url": "https://a.de/?hl=de", "name": "A"}}, live, {}) == {"a": "u1"}
    True

    Exactly one candidate, or none. Five Aldi Süd branches share a title, and picking one of
    them would silently rewrite a stranger's watch; a page still held by another entry is not
    free either.

    >>> two = {"u1": {"url": "https://a.de/", "title": "Aldi"},
    ...        "u2": {"url": "https://b.de/", "title": "Aldi"}}
    >>> adoptions({"a": {"url": "https://c.de/", "name": "Aldi"}}, two, {})
    {}
    >>> both = {"a": {"url": "https://a.de/", "name": "A"},
    ...         "b": {"url": "https://b.de/?neu", "name": "A"}}
    >>> adoptions(both, live, {}) == {"a": "u1"}
    True
    >>> adoptions({"a": {"url": "https://a.de/", "name": "A"}}, live, {"a": "u1"})
    {}
    """
    adopt = {}
    unlocked = [s for s in entries if lock.get(s) not in live]
    if not unlocked:
        return adopt
    taken = {lock[s] for s in entries if lock.get(s) in live}
    by_url = {}
    for u, w in live.items():
        by_url.setdefault(norm_url(w.get("url")), []).append(u)
    for slug in unlocked:
        free = [u for u in by_url.get(norm_url(entries[slug].get("url")), [])
                if u not in taken and u not in adopt.values()]
        if len(free) == 1:
            pick = free[0]
        else:
            want = norm_name(entries[slug].get("name"))
            exact = [u for u in free if norm_name(live[u].get("title")) == want]
            pick = exact[0] if len(exact) == 1 else None
        if pick:
            adopt[slug] = pick

    # Only watches that no entry addresses at all: a page some other entry still names is that
    # entry's, even when this pass runs before that entry has claimed it.
    wanted = {norm_url(e.get("url")) for e in entries.values()}
    pool = [u for u in live
            if u not in taken and norm_url(live[u].get("url")) not in wanted]
    for slug in unlocked:
        if slug in adopt:
            continue
        want = norm_name(entries[slug].get("name"))
        same = [u for u in pool
                if u not in adopt.values() and norm_name(live[u].get("title")) == want]
        if len(same) == 1:
            adopt[slug] = same[0]
    return adopt


def desired(entry, tag_uuids=None):
    """The watch fields this entry asserts."""
    want = {}
    if tag_uuids is not None:
        want["tags"] = tag_uuids
    for ef, wf in FIELD_MAP.items():
        if ef in entry:
            want[wf] = entry[ef]
    want["include_filters"] = [entry["filter"]] if entry.get("filter") else []
    # A per-watch body is the only place the OSM id fits: the cascade is watch -> tag -> global,
    # and a non-empty value here wins outright (no companion "use_default" flag, unlike
    # time_between_check). Entries without an osm_id fall through to the global body.
    # Winning outright means REPLACING it, not extending it, so every line the reader needs has
    # to stand here too. 558 of 559 entries carry an osm_id, which made the guidance in the
    # global body unreachable for all but one watch until this text was repeated.
    if entry.get("osm_id"):
        # `Hinweise:` separates the page from the guidance under it. The relay needs that line:
        # without it the guidance arrives as unmarked diff lines, which silences the rotation
        # note and lets a long diff push the instruction out. One thought per line, and each
        # link on a line of its own, because the relay renders a prose label as the link text.
        want["notification_body"] = (
            "Webseite: {{watch_url}}\n"
            f"OpenStreetMap: {OSM_BASE}/{entry['osm_id']}\n"
            "{{diff}}\n"
            "Hinweise:\n"
            "Zu tun: Zeiten in OSM prüfen, dann check_date:opening_hours setzen.\n"
            "Zeigt der Diff keine Öffnungszeiten, ist es ein Fehlalarm: OSM bleibt unberührt, "
            "der Filter muss nachgezogen werden.\n"
            f"Wie Fehlalarme entstehen: {DOCS_BASE}/notifications.md#umsortiert\n"
            # No `&url={{watch_url}}` prefill: an issue-form field is filled through the query
            # string, and a watch URL that carries one of its own (`?branch=500735` at Würth,
            # `?store=…` at brillen.de) would end the parameter early and arrive truncated.
            "Fehlalarm melden: "
            f"{REPO_BASE}/issues/new?template=filter-korrigieren.yml"
        )
    return want


def differs(want, live):
    out = {}
    for k, v in want.items():
        cur = live.get(k)
        if k in ("include_filters", "trigger_text", "subtractive_selectors", "extract_text",
                 "text_should_not_be_present"):
            cur = [x for x in (cur or []) if str(x).strip()]
            v = [x for x in (v or []) if str(x).strip()]
        if k == "tags":
            cur = sorted(cur or [])
            v = sorted(v or [])
        if k == "fetch_backend":
            cur = cur or "system"
            v = v or "system"
        if cur != v:
            out[k] = (cur, v)
    return out


def prune_urteil(anzahl_entries, anzahl_orphans, max_prune):
    """Should these unclaimed watches be deleted? -> (ok, reason).

    "No file claims this watch" and "the checkout is broken" look identical from here, so the
    count decides: a merged pull request removes a handful, an accident orphans everything.

    >>> prune_urteil(551, 2, 5)[0]          # a pull request removed two files
    True
    >>> prune_urteil(548, 5, 5)[0]          # exactly at the limit
    True
    >>> prune_urteil(547, 6, 5)[0]          # one over: refuse, and say so
    False
    >>> prune_urteil(0, 553, 5)[0]          # empty checkout: never
    False
    >>> prune_urteil(553, 0, 5)[0]          # nothing to do
    True
    """
    if anzahl_orphans == 0:
        return True, ""
    if anzahl_entries == 0:
        return False, ("Es wurde kein einziger Eintrag geladen. Ein leeres entries/ ist ein "
                       "kaputter Checkout und keine Aufforderung, alle Watches zu löschen.")
    if anzahl_orphans > max_prune:
        return False, (f"{anzahl_orphans} Watches beansprucht kein Eintrag mehr, erlaubt sind "
                       f"{max_prune}. War die Löschung so gewollt, den Lauf einmal mit einem "
                       f"höheren --max-prune wiederholen.")
    return True, ""


def prune_meldung(namen, geloescht, grund=""):
    """-> (title, body) for the relay. `namen` is a list of (title, url).

    The room reads German, and the relay turns leading "Label: <url>" lines into header links, so
    every watch named here is one click away. A deletion needs no explanation: it happens only
    when an entry file left main, and whoever removed it knows why. A refusal is the opposite,
    it asks somebody to act, so it carries the reason and what to do about it.

    >>> t, b = prune_meldung([("Studio by Laura", "https://bylaura.de/")], True)
    >>> t
    'Watch entfernt: Studio by Laura'
    >>> b.splitlines()[0]
    'Webseite: https://bylaura.de/'
    >>> prune_meldung([("A", "u1"), ("B", "u2")], True)[0]
    '2 Watches entfernt'
    >>> t, b = prune_meldung([("A", "u1")], False, "6 Watches sind zu viele.")
    >>> t
    'Sync hat nicht aufgeräumt'
    >>> b.splitlines()[-1]
    'Nichts wurde gelöscht.'
    """
    liste = ([f"Webseite: {namen[0][1]}"] if len(namen) == 1
             else [f"{n}: {u}" for n, u in namen[:20]])
    if len(namen) > 20:
        liste.append(f"… und {len(namen) - 20} weitere")
    if not geloescht:
        return ("Sync hat nicht aufgeräumt",
                "\n".join(liste + ["", grund, "Nichts wurde gelöscht."]))
    title = (f"Watch entfernt: {namen[0][0]}" if len(namen) == 1
             else f"{len(namen)} Watches entfernt")
    return (title, "\n".join(liste))


# A watch whose capture is redrawn compares a new excerpt against a snapshot taken through the
# old one, so its next check reports a difference that is not a change of hours. Nothing else in
# `desired()` does that: an interval or a title leaves the captured text alone.
BASELINE_KEYS = ("url", "include_filters", "subtractive_selectors", "extract_text",
                 "ignore_text", "fetch_backend", "sort_text_alphabetically",
                 "text_should_not_be_present")


def baseline_meldung(name, seite):
    """-> (title, body) for the note that goes in front of that difference.

    Sent before the recheck, so it stands in the room when the alert lands rather than after it.

    >>> t, b = baseline_meldung("Bücherei", "https://example.de/oeffnungszeiten")
    >>> t
    'Filter geändert: Bücherei'
    >>> b.splitlines()[0]
    'Webseite: https://example.de/oeffnungszeiten'
    """
    return (f"Filter geändert: {name}",
            "\n".join([
                f"Webseite: {seite}",
                "",
                "Der Eintrag trägt einen neuen Ausschnitt, die Seite wird sofort neu geprüft.",
                "Die Meldung darauf vergleicht den alten Ausschnitt mit dem neuen und ist "
                "keine geänderte Öffnungszeit. Danach zählt wieder jede Meldung."]))


def melden(url, title, body, **extra):
    """Say it where somebody reads it. Never fail the run over a failed notification.

    `extra` reaches the relay as it is: `thread` puts the message under the page's last alert,
    `expect_baseline` marks the alert that follows as the announced one.
    """
    if not url:
        return
    try:
        payload = json.dumps({"title": title, "message": body, **extra}).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"notified: HTTP {r.status}")
    except Exception as e:
        print(f"notify failed ({type(e).__name__}) — continuing", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Reconcile changedetection against entries/")
    ap.add_argument("--entries", default="entries")
    ap.add_argument("--lock", help="slug->uuid map (default: <entries>/.lock.json). One lock "
                                   "per changedetection INSTANCE — a local cluster and "
                                   "production hold different uuids for the same entries, so "
                                   "sharing a lock orphans one of them.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--prune", action="store_true",
                    help="delete watches that no entry claims (needs --apply)")
    ap.add_argument("--max-prune", type=int, default=5, metavar="N",
                    help="refuse to prune when more than N watches are unclaimed (default 5). "
                         "A pull request removes a handful; a broken checkout orphans "
                         "everything, and the two are told apart by the count alone.")
    ap.add_argument("--notify", metavar="URL", default=os.environ.get("RELAY_URL"),
                    help="post what was pruned, or why it was refused, to the relay. A Job log "
                         "is kept for three runs and read by nobody.")
    ap.add_argument("--base-url",
                    default=os.environ.get("CD_BASE_URL", "http://localhost:5000"),
                    help="changedetection API root; defaults to $CD_BASE_URL, so the same call "
                         "works in-cluster, on the VPS and through a tunnel "
                         "(see scripts/cd_env.sh)")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--container", default="changedetection")
    ap.add_argument("--tag", default="fulda", help="tag for newly created watches")
    ap.add_argument("--interval-days", type=int, default=3)
    args = ap.parse_args()

    api = C.CDIO(args.base_url, C.resolve_api_key(args.api_key, args.container))
    entries = load_entries(args.entries)
    lock_path = args.lock or os.path.join(args.entries, ".lock.json")
    lock = json.load(open(lock_path)) if os.path.exists(lock_path) else {}
    live_uuids = set((api.list() or {}).keys())

    def get(u):
        try:
            return u, api.get(u)
        except Exception:
            return u, None
    live = {}
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for u, w in ex.map(get, sorted(live_uuids)):
            if w:
                live[u] = w

    tag_cache = {t.get("title"): u for u, t in (api.tags() or {}).items() if t.get("title")}
    _resolved = {}

    def tags_for(entry):
        names = entry.get("tags") or []
        if not names:
            return None
        key = tuple(names)
        if key not in _resolved:
            _resolved[key] = resolve_tags(api, names, tag_cache, create=args.apply)
        return _resolved[key]

    adopt = adoptions(entries, live, lock)

    create, update, delete = [], [], []
    for slug, entry in entries.items():
        uuid = lock.get(slug)
        if uuid not in live:
            uuid = adopt.get(slug)
        if not uuid:
            create.append((slug, entry))
            continue
        d = differs(desired(entry, tags_for(entry)), live[uuid])
        if d:
            update.append((slug, uuid, d))

    claimed = {lock[s] for s in entries if s in lock} | set(adopt.values())
    for slug, uuid in lock.items():
        if slug not in entries and uuid in live:
            delete.append((slug, uuid))
    orphans = [u for u in live if u not in claimed and
               u not in {uu for _s, uu in delete}]

    print(f"entries {len(entries)} · live watches {len(live)}")
    print(f"create {len(create)} · update {len(update)} · delete {len(delete)} · "
          f"adopted {len(adopt)} · unclaimed {len(orphans)}")
    for slug in list(adopt)[:20]:
        print(f"  = {slug}  adopted {adopt[slug][:8]}  {live[adopt[slug]].get('title') or ''}")
    for slug, entry in create[:20]:
        print(f"  + {slug}  {entry.get('url','')[:60]}")
    uuid_to_name = {u: t.get("title") for u, t in (api.tags() or {}).items()}
    for slug, _u, d in update[:20]:
        for k, (cur, new) in d.items():
            if k == "tags":     # uuids are unreadable; the entry speaks in names
                cur = [uuid_to_name.get(x, x[:8]) for x in (cur or [])]
                new = [uuid_to_name.get(x, x[:8]) for x in (new or [])] or \
                      (entries[slug].get("tags") or [])
            print(f"  ~ {slug}  {k}: {str(cur)[:38]!r} -> {str(new)[:38]!r}")
    for slug, uuid in delete[:20]:
        print(f"  - {slug}  ({uuid[:8]})")
    for u in orphans[:10]:
        print(f"  ? unclaimed {u[:8]}  {live[u].get('title') or live[u].get('url','')[:50]}")

    if not args.apply:
        print("\n(plan only — pass --apply)")
        return 0

    # Adoptierte gehören ins Lock, damit es als Zwischenspeicher wieder stimmt — verlieren
    # darf man es jetzt aber, das ist der Punkt der Übung.
    lock.update(adopt)
    for slug, entry in create:
        uuid = api.add(entry["url"], args.tag, args.interval_days)
        api.update(uuid, **desired(entry, tags_for(entry)))
        lock[slug] = uuid
        print(f"created {slug} -> {uuid[:8]}")
    for slug, uuid, d in update:
        api.update(uuid, **{k: v for k, (_c, v) in d.items()})
        print(f"updated {slug}")
        # Announce, then force the check. Without the recheck the difference arrives whenever
        # the three-day cadence next comes round, long after the pull request that caused it is
        # out of everybody's head; with it, the note and the alert are minutes apart and sit in
        # one thread.
        if any(k in d for k in BASELINE_KEYS):
            entry = entries[slug]
            melden(args.notify, *baseline_meldung(entry.get("name") or slug,
                                                  entry.get("url", "")),
                   thread=True, expect_baseline=True)
            try:
                api.recheck(uuid)
                print(f"rechecked {slug}")
            except Exception as e:
                print(f"recheck failed for {slug} ({type(e).__name__}) — the next scheduled "
                      f"check does it", file=sys.stderr)
    for slug, uuid in delete:
        api.delete(uuid)          # ALWAYS targeted by uuid, never by tag
        lock.pop(slug, None)
        print(f"deleted {slug}")
    if args.prune and orphans:
        namen = [(live[u].get("title") or "ohne Titel", live[u].get("url", "")[:80])
                 for u in orphans]
        ok, grund = prune_urteil(len(entries), len(orphans), args.max_prune)
        if not ok:
            print(f"REFUSED to prune: {grund}", file=sys.stderr)
            melden(args.notify, *prune_meldung(namen, False, grund))
        else:
            for u in orphans:
                api.delete(u)
                print(f"pruned unclaimed {u[:8]}")
            melden(args.notify, *prune_meldung(namen, True))

    with open(lock_path, "w") as fh:
        json.dump(lock, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("lock updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
