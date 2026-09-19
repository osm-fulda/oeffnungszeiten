#!/usr/bin/env python3
"""
wizard_bot.py — run the filter wizard from a GitHub issue, so nobody has to fork.

Adding a watch means picking the filter that captures a page's opening hours, and that is the
one step of the contribution that needs code. This wraps `filter_wizard.py` for the workflows in
`.github/workflows/wizard*.yml`: the issue form carries URL, name and OSM id, the bot fetches the
page, posts the candidates as a comment, and turns `/pick N` into a pull request.

Three subcommands, all of them file-in/file-out so a workflow never has to pass untrusted text
through a shell:

  parse       read the issue body, print the fields as JSON (what the other two do first)
  candidates  fetch the page, write comment.md and candidates.json
  emit        fetch the page, take rank N, write the entry file, its PR body and meta.json

It writes no state anywhere else, holds no token, and talks to nothing but the page under
examination — the fetch runs in a job that has no write permission, and only its artifact
crosses into the job that can comment or push.

Examples:
  python3 scripts/wizard_bot.py candidates --body-file body.md --out out
  python3 scripts/wizard_bot.py emit --body-file body.md --pick 2 --out out
"""
import argparse
import calendar
import collections
import datetime
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.parse

import hours_lang as L
import osm_cd_common as C

# filter_wizard pulls in lxml. Everything that reads the issue works without it, and CI checks
# that half in the job that installs nothing, so the import waits until a page is fetched.

# The issue form's headings. GitHub renders one "### <label>" per field, in this order, and
# writes "_No response_" where an optional field was left empty.
FIELDS = {
    "Seite mit den Öffnungszeiten": "url",
    "Name des Betriebs": "name",
    "OSM-Objekt": "osm_id",
    "Kategorie-Tag": "tags",
    "Sprache der Seite": "lang",
    # the correction form: everything else about the entry is already on disk
    "Seite aus der Alarmnachricht": "url",
    "Was die Nachricht zeigte": "diff",
    # the removal form: the page, and what a person sees on it instead of hours
    "Seite, die keine Zeiten mehr führt": "url",
    "Was auf der Seite steht": "note",
    "Warum die Seite nichts mehr hergibt": "reason",
}

# The removal form's dropdown. A reporter reads a page, not our block list, so the form offers
# sentences and this maps them onto the reasons `no-watch.json` allows. Only the reasons a
# person can *see* are here: `anti-bot` and `datacenter-block` are properties of our instance,
# invisible from a home connection, and stay a maintainer's judgement.
REASONS = {
    "Die Seite nennt keine Öffnungszeiten (mehr)": "no-hours-on-page",
    "Nur nach Vereinbarung, feste Zeiten gibt es nicht": "appointment-only",
    "Die Zeiten stehen nur bei Facebook oder Instagram": "social-only",
    "Nur Lieferzeiten auf einer Lieferplattform": "delivery-platform-only",
    "Die Seite nennt nur den heutigen Tag": "today-only",
    "Durchgehend geöffnet, die Zeiten ändern sich nicht": "always-open",
    "Die Seite gibt es nicht mehr": "site-unreachable",
}
# `always-open` cannot move: the hours are known and constant. Everything else here is a
# property of the business, and a business changes its mind — half a year is what the list has
# used for that since it exists.
RECHECK_MONTHS = 6
NO_RESPONSE = "_no response_"
OSM_ID = re.compile(r"^(node|way|relation)/[1-9][0-9]*$")
TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
PICK = re.compile(r"^\s*/pick\s+([0-9]{1,2})\s*$", re.M)
# Parameters that identify a campaign, not a page. Everything else stays in the key: a query
# string is load-bearing here — `?branch=500735` is Würth's Fulda branch, `?store=221186` is a
# brillen.de shop, and dropping those would make seven pages look like their own front page.
TRACKING = re.compile(r"^(utm_\w+|gclid|fbclid|msclkid|igshid|mc_[ce]id|_ga)$")
# Ten, not six: on a page listing many branches the six widest captures win on score and
# every single-branch block falls off the list. wemag.de/de/unternehmen/ carries 21
# locations and the watched shop ranked 8th.
MAX_CANDIDATES = 10
MAX_TEXT = 600
# The pasted alarm is quoted whole, not folded into a paragraph: it is the evidence the reader
# compares the candidates against, and both halves of a changed line have to stay under each
# other. Wider than MAX_TEXT because it carries the old text and the new one, and NKD's sale
# boilerplate alone is 1800 characters - exactly the alarm somebody reports.
MAX_DIFF = 4000


class Refused(Exception):
    """Something in the issue cannot be worked with. The message goes back as a comment."""


def parse_body(body):
    r"""Issue-form markdown -> {url, name, osm_id, tags, lang}. Unknown headings are ignored.

    >>> b = "### Name des Betriebs\n\nCaf\u00e9 `X`\n\n### OSM-Objekt\n\n_No response_\n"
    >>> parse_body(b) == {"name": "Caf\u00e9 `X`", "osm_id": None}
    True
    >>> parse_body("### Sonst noch etwas\n\nzwei Betriebe auf der Seite\n")
    {}
    """
    out, key = {}, None
    for line in body.splitlines():
        head = line.strip()
        if head.startswith("###"):
            key = FIELDS.get(head.lstrip("#").strip())
            continue
        if key and head:
            out.setdefault(key, []).append(head)
    fields = {k: " ".join(v).strip() for k, v in out.items()}
    return {k: (None if not v or v.lower() == NO_RESPONSE else v) for k, v in fields.items()}


def raw_section(body, heading):
    r"""One issue-form section with its line breaks kept.

    `parse_body` folds a section into one line, which is right for a name and wrong for a diff:
    the two halves of `- alt` / `+ neu` are only readable underneath each other.

    >>> raw_section("### D\n\n- a\n+ b\n\n### E\n\nx\n", "D")
    '- a\n+ b'
    """
    out, hit = [], False
    for line in body.splitlines():
        if line.strip().startswith("###"):
            hit = line.lstrip("#").strip() == heading
            continue
        if hit:
            out.append(line.rstrip())
    text = "\n".join(out).strip()
    return "" if text.lower() == NO_RESPONSE else text


def check_url(raw):
    r"""A URL we are willing to fetch. Public http(s) only.

    The job doing the fetching has no token and no route to anything of ours, so this is not
    what keeps the cluster safe — that is the workflow's permissions. It keeps the bot from
    being pointed at a runner's own network as a matter of course, and it turns a typo into a
    sentence the reporter can act on instead of a stack trace.

    The scheme is judged before normalisation, because `normalize_url` prepends `https://` to
    anything that does not already carry a scheme it knows: `ftp://x.de` would otherwise become
    `https://ftp://x.de`, a fetch of the host `ftp`.

    >>> check_url("ftp://example.de")
    Traceback (most recent call last):
    wizard_bot.Refused: Nur http und https, nicht `ftp`.
    >>> check_url("")
    Traceback (most recent call last):
    wizard_bot.Refused: Es fehlt die URL der Seite.
    """
    if not raw:
        raise Refused("Es fehlt die URL der Seite.")
    raw = raw.split()[0]
    scheme = re.match(r"([a-zA-Z][a-zA-Z0-9+.-]*)://", raw)
    if scheme and scheme.group(1).lower() not in ("http", "https"):
        raise Refused(f"Nur http und https, nicht `{scheme.group(1).lower()}`.")
    url = C.normalize_url(raw)
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host:
        raise Refused(f"Keine Adresse in `{raw}` erkennbar.")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as e:
        raise Refused(f"`{host}` ist nicht auflösbar ({e.strerror or e}).")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise Refused(f"`{host}` zeigt auf eine nicht öffentliche Adresse ({ip}).")
    return url


def check_meta(f):
    """Everything about the entry that needs no network, checked on its own so CI can too.

    >>> check_meta({"name": "Café X", "osm_id": "https://www.openstreetmap.org/node/42"})["osm_id"]
    'node/42'
    >>> check_meta({"name": "X", "tags": "fulda-cafe, fulda"})["tags"]
    ['fulda-cafe', 'fulda']
    >>> check_meta({"name": "X"})["lang"]
    'de'
    >>> check_meta({"name": ""})
    Traceback (most recent call last):
    wizard_bot.Refused: Es fehlt der Name des Betriebs.
    >>> check_meta({"name": "X", "osm_id": "12345"})
    Traceback (most recent call last):
    wizard_bot.Refused: `12345` ist keine OSM-Id in der Form `node/123456`.
    >>> check_meta({"name": "X", "tags": "Fulda Bäckerei"})
    Traceback (most recent call last):
    wizard_bot.Refused: `Fulda` ist kein brauchbarer Tag (klein, ohne Leerzeichen, z. B. `fulda-bakery`).
    >>> check_meta({"name": "X", "lang": "fr"})
    Traceback (most recent call last):
    wizard_bot.Refused: Sprache `fr` kennt der Wizard nicht, nur `de` und `en`.
    """
    name = (f.get("name") or "").strip()
    if not name:
        raise Refused("Es fehlt der Name des Betriebs.")
    if len(name) > 80:
        raise Refused("Der Name ist länger als 80 Zeichen.")
    osm_id = (f.get("osm_id") or "").strip() or None
    if osm_id:
        # accept the whole openstreetmap.org address, since that is what the browser offers
        osm_id = "/".join(osm_id.rstrip("/").split("/")[-2:]) if "/" in osm_id else osm_id
        if not OSM_ID.match(osm_id):
            raise Refused(f"`{osm_id}` ist keine OSM-Id in der Form `node/123456`.")
    tags = [t.strip() for t in re.split(r"[,\s]+", f.get("tags") or "") if t.strip()]
    for t in tags:
        if not TAG.match(t):
            raise Refused(f"`{t}` ist kein brauchbarer Tag (klein, ohne Leerzeichen, "
                          f"z. B. `fulda-bakery`).")
    lang = (f.get("lang") or "de").strip().lower()[:2]
    if lang not in ("de", "en"):
        raise Refused(f"Sprache `{lang}` kennt der Wizard nicht, nur `de` und `en`.")
    return {"name": name, "osm_id": osm_id, "tags": tags, "lang": lang}


def check_fields(f):
    """The URL (needs DNS) and everything else (does not)."""
    return {"url": check_url(f.get("url")), **check_meta(f)}


def known_tags(entries="entries"):
    """{tag: how many watches carry it}. The vocabulary is the file tree, not a list to maintain."""
    seen = collections.Counter()
    for name in sorted(os.listdir(entries)) if os.path.isdir(entries) else []:
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            e = json.load(open(os.path.join(entries, name)))
        except Exception:
            continue
        seen.update(e.get("tags") or [])
    return seen


def area_prefix(seen):
    """The area every tag starts with, read off the existing ones.

    Not a constant: the prefix is what separates one district's watches from another's, so a
    second Landkreis in the same instance brings its own. Deriving it means the bot says
    `fulda-` here and the right thing elsewhere, with nothing to keep in sync.

    >>> import collections
    >>> area_prefix(collections.Counter({"fulda-bakery": 22, "fulda-cafe": 15}))
    'fulda'
    >>> area_prefix(collections.Counter()) is None
    True
    """
    parts = collections.Counter(t.split("-")[0] for t in seen.elements() if "-" in t)
    return parts.most_common(1)[0][0] if parts else None


def tag_note(tags, entries="entries"):
    """A warning for a category tag nothing else uses, or [].

    Not a refusal: a genuinely new category is normal, a slipped one is not, and only a person
    can tell them apart. But an unnoticed one-off splits the grouping in two, and the list has
    56 tags with a single watch to show for it — including one that was the OSM *key*, and
    therefore fitted every shop in town.
    """
    seen = known_tags(entries)
    unknown = [t for t in tags if t not in seen]
    if not unknown:
        return []
    area = area_prefix(seen)
    common = ", ".join(f"`{t}` ({n})" for t, n in seen.most_common(8))
    rule = (f"Gemeint ist das Gebiet und der OSM-Wert der Kategorie, hier also `{area}-` und "
            f"der Wert: `shop=florist` wird zu `{area}-florist`. Nimm den Wert, nicht den "
            f"Schlüssel, denn `{area}-shop` passt auf jeden Laden."
            if area else
            "Gemeint ist das Gebiet und der OSM-Wert der Kategorie, etwa `fulda-florist` für "
            "`shop=florist` in Fulda.")
    lead = ("Diesen Tag trägt bisher kein Watch" if len(unknown) == 1
            else "Diese Tags trägt bisher kein Watch")
    return [f"⚠ {lead}: {', '.join('`' + t + '`' for t in unknown)}. "
            f"{rule} Gebräuchlich sind: {common}. Eine neue Kategorie oder ein neues Gebiet "
            f"ist in Ordnung, sag es dann kurz im Pull Request."]


def blocked(url, path="no-watch.json"):
    """The no-watch record for this page, if there is one, plus whether it is due again.

    The block list is the other half of `entries/`: a page somebody already looked at and found
    nothing worth watching on, with the reason and a date to look again. Proposing it a second
    time is the exact thing that list exists to prevent — and CI would refuse the pull request
    anyway, because a page may sit in only one of the two lists. Better to say so before the
    branch exists, with the reason and the date rather than a rule number.
    """
    try:
        records = json.load(open(path)).get("records", [])
    except Exception:
        return None, False
    want = page_key(url)
    for r in records:
        if page_key(r.get("url") or "") == want:
            due = bool(re.match(r"^20\d\d-\d\d-\d\d$", r.get("recheck") or "")) \
                and r["recheck"] <= time.strftime("%Y-%m-%d")
            return r, due
    return None, False


def drop_block(url, path="no-watch.json"):
    """The block list without this page, as text. Used when a due record is being replaced."""
    doc = json.load(open(path))
    want = page_key(url)
    doc["records"] = [r for r in doc["records"] if page_key(r.get("url") or "") != want]
    return json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def recheck_date(reason, today=None):
    """When to look at this page again — a date, or `never` for what cannot change.

    >>> recheck_date("no-hours-on-page", datetime.date(2026, 8, 31))
    '2027-02-28'
    >>> recheck_date("today-only", datetime.date(2026, 12, 31))
    '2027-06-30'
    >>> recheck_date("always-open", datetime.date(2026, 8, 31))
    'never'
    """
    if reason == "always-open":
        return "never"
    d = today or datetime.date.today()
    month = d.month - 1 + RECHECK_MONTHS
    year, month = d.year + month // 12, month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day).isoformat()


def block_record(entry, reason, note, issue):
    """The `no-watch.json` record for a watch that is being removed.

    Everything but the reason and the note comes off the entry, so the form can ask for what a
    person sees on the page and nothing else. `captured_sample` is carried into the note on
    purpose: it is the last thing the watch actually caught, and deleting the file is the moment
    it would otherwise be lost — the same shape the hand-written records use.
    """
    note = " ".join((note or "").split())
    sample = " ".join((entry.get("captured_sample") or "").split())
    if sample:
        note += f" Zuletzt erfasst, bevor der Watch entfernt wurde: {sample}"
    if issue:
        note += f" (Issue #{issue})"
    return {
        "url": entry.get("url"),
        "name": entry.get("name"),
        "reason": reason,
        "established": datetime.date.today().isoformat(),
        "recheck": recheck_date(reason),
        "note": note.strip(),
        "osm_id": entry.get("osm_id"),
    }


def add_block(record, path="no-watch.json"):
    """The block list with this page added, as text.

    Written without `sort_keys`, unlike `drop_block`: the file carries its keys in reading order
    (url, name, reason, …), and sorting them would rewrite all 281 records for the sake of one.
    A reviewer should see a single added record and nothing else.
    """
    doc = json.load(open(path))
    doc["records"] = [r for r in doc["records"]
                      if page_key(r.get("url") or "") != page_key(record["url"])] + [record]
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


def drop_pr_body(entry, path, record, issue):
    return "\n".join([
        f"**{entry.get('name')}** wird nicht mehr beobachtet, gemeldet in #{issue}.",
        "",
        f"- Seite: {entry.get('url')}",
        f"- Grund: `{record['reason']}`",
        f"- Wieder ansehen: `{record['recheck']}`",
        "",
        "Das steht künftig in `no-watch.json`:",
        "",
        fence(record["note"]),
        "",
        f"`{path}` fällt weg, der Eintrag in `no-watch.json` tritt an seine Stelle — eine Seite "
        f"gehört in genau eine der beiden Listen, und CI prüft das. Nach dem Merge löscht die "
        f"Sync den Watch binnen einer Stunde und meldet es in Matrix.",
        "",
        f"Bitte vor dem Merge selbst auf die Seite sehen: der Bot hat sie **nicht** abgerufen, "
        f"hier steht, was ein Mensch gelesen hat.",
        "",
        f"Closes #{issue}",
    ])


def page_key(url):
    """What makes two links the same page, for the duplicate check.

    A person pastes what the address bar shows, and that carries differences the site does not
    have: `www.`, `http` against `https`, a trailing slash, a campaign parameter, the `#anchor`
    the browser jumped to. Comparing raw strings therefore misses duplicates a reader would call
    obvious. Case is folded too: a path differing only in case is a duplicate far more often
    than it is a second page.

    >>> page_key("https://www.x.de/Kontakt/") == page_key("http://x.de/kontakt")
    True
    >>> page_key("https://x.de/k?utm_source=nl&store=7") == page_key("https://x.de/k?store=7")
    True
    >>> page_key("https://x.de/#oeffnungszeiten") == page_key("https://x.de/")
    True
    >>> page_key("https://x.de/a") == page_key("https://x.de/b")
    False
    """
    s = urllib.parse.urlsplit(C.normalize_url(url))
    host = (s.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    query = "&".join(sorted(
        f"{k}={v}" for k, v in urllib.parse.parse_qsl(s.query, keep_blank_values=True)
        if not TRACKING.match(k.lower())))
    return host + s.path.rstrip("/").lower() + (f"?{query}" if query else "")


def already_watched(url, osm_id=None, entries="entries"):
    """(filename, "page"|"osm") of the entry that already covers this, or (None, None).

    Two keys, because a duplicate arrives in two shapes. The page catches the same link pasted
    again; the OSM object catches the same business proposed through another of its pages,
    which no URL comparison can see. A second watch on one page is a real failure mode here:
    two businesses sharing a page once shared a single watch, and each new file makes that
    harder to notice.
    """
    if not os.path.isdir(entries):
        return None, None
    want = page_key(url)
    for name in sorted(os.listdir(entries)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            e = json.load(open(os.path.join(entries, name)))
        except Exception:
            continue
        if page_key(e.get("url") or "") == want:
            return name, "page"
        if osm_id and e.get("osm_id") == osm_id:
            return name, "osm"
    return None, None


def watching(url, entries="entries"):
    """Every entry file whose page is this one — usually one, sometimes two.

    Two businesses on one page is a known shape (FILTERS.md case 12), and removing one of that
    pair is not a decision the removal form can make: the page still carries hours, only not
    for this business. So the bot counts rather than assumes, and hands the pair to a person.
    """
    if not os.path.isdir(entries):
        return []
    want = page_key(url)
    hits = []
    for name in sorted(os.listdir(entries)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            e = json.load(open(os.path.join(entries, name)))
        except Exception:
            continue
        if page_key(e.get("url") or "") == want:
            hits.append(name)
    return hits


def fetch(url, lang):
    """(html, ranked candidates). Raises Refused with a readable reason."""
    import filter_wizard as W
    try:
        html = W.fetch_plain(url)
    except Exception as e:
        raise Refused(f"Die Seite war nicht abrufbar: `{e}`. Antwortet sie im Browser, "
                      f"blockt der Anbieter vermutlich Rechenzentren — dann trägt der Fall "
                      f"in `blocked-hosts.txt` mehr als ein Watch.")
    ranked = W.collect(html, lang)
    real = [c for c in ranked if c["strategy"] != "whole page"]
    if not real:
        raise Refused(
            "Im ausgelieferten HTML stehen keine Öffnungszeiten.\n\n"
            "Zwei Ursachen, beide häufig: die Zeiten stehen auf einer anderen Unterseite "
            "(`/kontakt`, `/oeffnungszeiten`, bei Ketten die Filialseite), oder sie erscheinen "
            "erst, wenn JavaScript gelaufen ist. Der erste Fall ist der häufigere — gemessen "
            "11 von 12. Probier die Unterseite und öffne ein neues Issue damit. Für den "
            "zweiten Fall braucht es einen Browser, den dieser Bot nicht hat.")
    return html, ranked


def unfence(text):
    r"""Drop a code fence the issue form already put around a pasted block.

    A `render: text` field arrives fenced from GitHub. Fencing it a second time shows the inner
    ```text as a line of its own and puts the reader in front of markup instead of the alarm.

    >>> unfence("```text\nMo 9-17\n```")
    'Mo 9-17'
    >>> unfence("Mo 9-17")
    'Mo 9-17'
    """
    lines = text.strip().splitlines()
    if len(lines) >= 2 and re.match(r"^`{3,}\w*$", lines[0].strip()) \
            and re.match(r"^`{3,}$", lines[-1].strip()):
        return "\n".join(lines[1:-1]).strip()
    return text.strip()


def fence(text, limit=MAX_TEXT, keep_lines=False):
    """A fenced block that survives backticks in the captured text.

    `keep_lines` quotes the text as it stands. Captured page text is folded into one paragraph,
    because its line breaks are an accident of the markup; a pasted alarm is not folded, because
    there they carry the meaning. Either way a cut is named rather than left to look like the
    end of the text.
    """
    body = unfence(text) if keep_lines else re.sub(r"\s{2,}", " ", text).strip()
    cut = ""
    if len(body) > limit:
        body, cut = body[:limit].rstrip(), f"\n[…] gekürzt, {len(body) - limit} Zeichen mehr"
    ticks = "`" * max(3, max((len(m) for m in re.findall(r"`+", body)), default=0) + 1)
    return f"{ticks}\n{body}{cut}\n{ticks}"


def candidate_block(i, cand, lang):
    import filter_wizard as W

    days = L.days_phrase(cand.get("days"), lang)
    head = f"### [{i}] {cand['strategy']}"
    meta = f"{days} · {len(cand['text'])} Zeichen · `{cand['filter'] or 'kein Filter'}`"
    if cand.get("context"):
        hits = cand.get("matches", 1)
        meta += (f"\nunter der Überschrift **{cand['context']}**" if hits < 2 else
                 f"\n{hits} Treffer auf der Seite, der erste unter **{cand['context']}**")
    lines = [head, meta, "", fence(cand["text"]), ""]
    lines += [f"- ! {f}" for f in cand.get("flags", [])]
    lines.append(f"- → {W.verdict(cand)}")
    return "\n".join(lines)


def candidates_comment(f, ranked, html_len, notes=()):
    shown = ranked[:MAX_CANDIDATES]
    parts = [
        f"Abgerufen: {f['url']} ({html_len} Bytes, einfacher Abruf, kein Browser).",
        "",
        *([*notes, ""] if notes else []),
        "**Nicht der Selektor entscheidet, sondern der Text.** Lies die Blöcke und sag, welcher "
        "genau die Öffnungszeiten dieses Betriebs enthält — nicht die Zeiten eines "
        "Terminformulars, nicht die einer Nachbarfiliale, nicht eine Uhr, die jede Minute "
        "weiterläuft. Die Reihenfolge ist ein Vorschlag, kein Urteil.",
        "",
    ]
    parts += [candidate_block(i, c, f["lang"]) + "\n" for i, c in enumerate(shown, 1)]
    parts += [
        "",
        "---",
        "",
        f"Antworte mit `/pick N`, etwa `/pick 1`, dann baue ich den Eintrag und öffne den "
        f"Pull Request. Passt keiner, ist meist die Seite falsch: viele Betriebe führen ihre "
        f"Zeiten auf `/kontakt` oder auf der Filialseite.",
    ]
    return "\n".join(parts)


def fix_comment(entry, path, current, diff, ranked, lang, html_len, url):
    """The correction comment: what the filter grabs now, what was reported, then the menu.

    The comparison is the whole point. A wandering "today" block is invisible in a single
    capture and obvious the moment the current text sits above a candidate without it.
    """
    now = ("nicht ausgewertet (`json:`- und CSS-Filter kann ich hier nicht anwenden)"
           if current is None else fence(current) if current else
           "**nichts** — der Filter trifft auf dieser Seite nicht mehr zu")
    parts = [
        f"Abgerufen: {url} ({html_len} Bytes, einfacher Abruf, kein Browser).",
        f"Beobachtet wird die Seite als `{path}`.",
        "",
        f"**Was der Filter bisher erfasste** (`{entry.get('filter') or 'kein Filter'}`):",
        "",
        now,
        "",
    ]
    if diff:
        parts += ["**Was in der Nachricht stand:**", "",
                  fence(diff, limit=MAX_DIFF, keep_lines=True), ""]
    parts += [
        "**So liest du die Liste ⬇️:**",
        "",
        "- Unterscheidet sich ein Kandidat vom Text oben nur durch etwas am Anfang oder Ende, "
        "das zum heutigen Wochentag passt, ist das die Ursache — nimm ihn.",
        "- Zeigen alle Kandidaten denselben Inhalt und wechselt nur die Reihenfolge, ist es "
        "keine Filterfrage, sondern Sortierung. Schreib das ins Issue statt einen Kandidaten "
        "zu wählen; ein Maintainer prüft es mit `rotation_check.py` gegen die gespeicherten "
        "Snapshots. Ein enger gezogener Filter würde es hier schlimmer machen.",
        "",
    ]
    for i, c in enumerate(ranked[:MAX_CANDIDATES], 1):
        block = candidate_block(i, c, lang)
        if c["filter"] and c["filter"] == entry.get("filter"):
            block += "\n- ℹ das ist der Filter, der jetzt schon eingetragen ist"
        parts.append(block + "\n")
    parts += [
        "",
        "---",
        "",
        "Antworte mit `/pick N`, dann schreibe ich den neuen Filter in die vorhandene Datei. "
        "`name`, `osm_id`, `tags` und `added` bleiben, `captured_sample` und `note` ziehe ich nach.",
    ]
    return "\n".join(parts)


def fix_entry(outdir, path, entry, cand, issue):
    """Write the corrected entry: the filter and the two fields that document it."""
    out = dict(entry)
    out["filter"] = cand["filter"]
    out["captured_sample"] = " ".join(cand["text"].split())[:200]
    stamp = datetime.date.today().isoformat()
    was = (out.get("note") or "").strip()
    add = (f"{stamp}: Filter auf {cand['strategy']} geändert, der vorherige erfasste "
           f"seitenabhängigen Text mit (Issue #{issue}).")
    out["note"] = f"{was} {add}".strip()
    os.makedirs(outdir, exist_ok=True)
    dest = os.path.join(outdir, os.path.basename(path))
    with open(dest, "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    return dest


def fix_pr_body(entry, path, cand, current, issue):
    import filter_wizard as W
    return "\n".join([
        f"Filter für **{entry.get('name')}** korrigiert, gemeldet in #{issue}.",
        "",
        f"- Seite: {entry.get('url')}",
        f"- Bisher: `{entry.get('filter')}`",
        f"- Neu: `{cand['filter'] or 'kein Filter (ganze Seite)'}`",
        "",
        "Das fing der bisherige Filter beim Abruf:",
        "",
        fence(current or "(nicht ausgewertet)"),
        "",
        "Das fängt der neue:",
        "",
        fence(cand["text"]),
        "",
        *[f"- ! {x}" for x in cand.get("flags", [])],
        f"- → {W.verdict(cand)}",
        "",
        f"Datei: `{path}`. Nach dem Merge zieht die Sync den Filter nach; danach folgt **ein** "
        f"Alarm, weil der erste Snapshot mit dem neuen Filter gegen den alten läuft.",
        "",
        f"Closes #{issue}",
    ])


def pr_body(f, cand, path, issue, notes=()):
    import filter_wizard as W
    return "\n".join([
        f"Watch für **{f['name']}**, vorgeschlagen in #{issue}.",
        "",
        f"- Seite: {f['url']}",
        f"- OSM: {'https://www.openstreetmap.org/' + f['osm_id'] if f['osm_id'] else '—'}",
        f"- Filter: `{cand['filter'] or 'kein Filter (ganze Seite)'}`",
        "",
        "Das fängt der Filter heute:",
        "",
        fence(cand["text"]),
        "",
        *[f"- ! {x}" for x in cand.get("flags", [])],
        f"- → {W.verdict(cand)}",
        "",
        f"Datei: `{path}`. Die Prüfung in CI holt die Seite noch einmal und hält den Filter "
        f"dagegen; was hier steht, ist der Stand des Abrufs von eben.",
        "",
        *([*notes, ""] if notes else []),
        f"Closes #{issue}",
    ])


def compare_url(repo, branch, title, body, base="main"):
    """A link that opens GitHub's pull-request form with everything filled in.

    The fallback for the day Actions is not allowed to open the pull request itself — that is a
    repository setting, off by default. One click by a human replaces it, and it buys something
    the automatic path does not have: a pull request opened by a person runs CI, while one
    opened with GITHUB_TOKEN starts no further workflow.

    >>> compare_url("o/r", "watch/x", "watch: X & Y", "b")
    'https://github.com/o/r/compare/main...watch/x?quick_pull=1&title=watch%3A+X+%26+Y&body=b'
    """
    q = urllib.parse.urlencode({"quick_pull": 1, "title": title, "body": body[:4000]})
    return f"https://github.com/{repo}/compare/{base}...{branch}?{q}"


def write(out, name, text):
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, name)
    with open(path, "w") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(description="Run the filter wizard from a GitHub issue")
    ap.add_argument("command", choices=["parse", "candidates", "emit", "compare"])
    ap.add_argument("--body-file", required=True,
                    help="file holding the issue body, or the pull-request body for `compare`")
    ap.add_argument("--repo", help="owner/name, for `compare`")
    ap.add_argument("--branch", help="branch the entry sits on, for `compare`")
    ap.add_argument("--title", help="pull-request title, for `compare`")
    ap.add_argument("--comment-file", help="file holding the /pick comment (emit)")
    ap.add_argument("--pick", type=int, help="rank to take (emit, overrides --comment-file)")
    ap.add_argument("--issue", type=int, default=0, help="issue number, for the PR body")
    ap.add_argument("--out", default="out", help="directory for the generated files")
    ap.add_argument("--fix", action="store_true",
                    help="correct the filter of an existing entry instead of creating one")
    ap.add_argument("--drop", action="store_true",
                    help="remove an existing entry and record the page in the block list")
    ap.add_argument("--entries", default="entries",
                    help="checked for a watch on the same page (default: %(default)s)")
    ap.add_argument("--absences", default="no-watch.json",
                    help="the block list, checked for this page too (default: %(default)s)")
    args = ap.parse_args()

    body = open(args.body_file, encoding="utf-8", errors="replace").read()
    if args.command == "compare":
        url = compare_url(args.repo, args.branch, args.title, body)
        write(args.out, "compare.md",
              f"Der Branch steht: [`{args.branch}`](https://github.com/{args.repo}/tree/"
              f"{args.branch}). Den Pull Request musst du selbst öffnen, ein Klick:\n\n"
              f"**[Pull Request anlegen]({url})**\n\n"
              f"Titel und Text sind vorausgefüllt. Actions darf hier keine Pull Requests "
              f"anlegen — das ist eine Repo-Einstellung (Settings → Actions → General → "
              f"Workflow permissions). Der Klick hat einen eigenen Vorteil: ein von einem "
              f"Menschen geöffneter Pull Request löst CI aus, ein von `GITHUB_TOKEN` "
              f"geöffneter nicht.")
        return 0
    try:
        raw = parse_body(body)
        if args.drop:
            # Nothing here is fetched and nothing is guessed: the page identifies the entry,
            # and everything the record needs beyond the reason and the note is already in it.
            f = {"url": check_url(raw.get("url")), "reason": raw.get("reason"),
                 "note": raw_section(body, "Was auf der Seite steht")}
        elif args.fix:
            # Name, OSM id and tags are already on disk; asking for them again would invite a
            # second, contradicting answer. Only the page and the reported diff come from here.
            f = {"url": check_url(raw.get("url")), "lang": "de",
                 "diff": raw_section(body, "Was die Nachricht zeigte")}
        else:
            f = check_fields(raw)
        if args.command == "parse":
            print(json.dumps(f, ensure_ascii=False, indent=1))
            return 0

        if args.drop:
            hits = watching(f["url"], args.entries)
            if not hits:
                raise Refused(
                    "Diese Seite steht nicht in `entries/`, es gibt also keinen Watch zu "
                    "entfernen. Prüf die Adresse aus der Alarmnachricht — steht die Seite auf "
                    "der Sperrliste, ist sie schon aus dem Rennen.")
            if len(hits) > 1:
                liste = ", ".join(f"`entries/{h}`" for h in hits)
                raise Refused(
                    f"Auf dieser Seite liegen zwei Watches: {liste}. Die Seite trägt dann "
                    f"Zeiten für zwei Betriebe, und welcher davon wegfällt, kann ich nicht "
                    f"entscheiden. Sag es hier im Issue, ein Maintainer macht es von Hand.")
            reason = REASONS.get((f.get("reason") or "").strip())
            if not reason:
                raise Refused(
                    f"Den Grund {f.get('reason')!r} kenne ich nicht. Nimm einen aus der Liste "
                    f"im Formular — jeder steht für einen Eintrag, den `no-watch.json` "
                    f"zulässt.")
            note = " ".join((f.get("note") or "").split())
            if len(note) < 30:
                raise Refused(
                    "Die Notiz ist zu kurz. Sie ist das Einzige, was in einem halben Jahr noch "
                    "erklärt, warum hier nichts beobachtet wird — schreib in einem Satz, was "
                    "auf der Seite **statt** der Zeiten steht. CI verlangt 30 Zeichen.")
            path = os.path.join(args.entries, hits[0])
            entry = json.load(open(path, encoding="utf-8"))
            record = block_record(entry, reason, note, args.issue)
            slug = hits[0][:-len(".json")]
            write(args.out, "no-watch.json", add_block(record, args.absences))
            write(args.out, "pr-body.md",
                  drop_pr_body(entry, f"entries/{hits[0]}", record, args.issue))
            write(args.out, "meta.json", json.dumps(
                {"slug": slug, "name": entry.get("name"), "url": entry.get("url"),
                 "branch": f"weg/{slug}", "remove": f"entries/{hits[0]}",
                 "title": f"no-watch: {entry.get('name')}"}, ensure_ascii=False, indent=1))
            return 0

        pick = args.pick
        if args.command == "emit" and not pick:
            m = PICK.search(open(args.comment_file, encoding="utf-8", errors="replace").read())
            if not m:
                raise Refused("Ich lese in dem Kommentar kein `/pick N`.")
            pick = int(m.group(1))

        if args.fix:
            dup, _ = already_watched(f["url"], None, args.entries)
            if not dup:
                raise Refused(
                    "Diese Seite steht nicht in `entries/`, es gibt also keinen Filter zu "
                    "korrigieren. Für eine neue Seite ist das Formular **Watch vorschlagen** "
                    "das richtige.")
            path = os.path.join(args.entries, dup)
            entry = json.load(open(path, encoding="utf-8"))
            lang = entry.get("lang") or f["lang"]
            html, ranked = fetch(f["url"], lang)
            import filter_wizard as W
            current = W.capture(html, entry.get("filter"))
            if args.command == "candidates":
                write(args.out, "comment.md",
                      fix_comment(entry, f"entries/{dup}", current, f.get("diff"), ranked,
                                  lang, len(html), f["url"]))
                write(args.out, "candidates.json", json.dumps(ranked, ensure_ascii=False,
                                                              indent=1))
                return 0
            if not 1 <= pick <= min(len(ranked), MAX_CANDIDATES):
                raise Refused(f"`{pick}` stand nicht zur Wahl, gezeigt waren "
                              f"{min(len(ranked), MAX_CANDIDATES)} Kandidaten. Setz das Label "
                              f"neu und such aus der neuen Liste.")
            cand = ranked[pick - 1]
            slug = dup[:-len(".json")]
            dest = fix_entry(os.path.join(args.out, "entry"), path, entry, cand, args.issue)
            write(args.out, "pr-body.md",
                  fix_pr_body(entry, f"entries/{dup}", cand, current, args.issue))
            write(args.out, "meta.json", json.dumps(
                {"slug": slug, "name": entry.get("name"), "url": entry.get("url"), "pick": pick,
                 "branch": f"filter/{slug}", "entry": dest, "drops_block": False,
                 "title": f"filter: {entry.get('name')}"}, ensure_ascii=False, indent=1))
            return 0

        record, due = blocked(f["url"], args.absences)
        if record and not due:
            when = {"never": "nie wieder", "on-relocation": "erst bei einem Standortwechsel"}.get(
                record.get("recheck"), f"wieder anzusehen ab {record.get('recheck')}")
            raise Refused(
                f"Diese Seite steht auf der Sperrliste, seit {record.get('established')}, "
                f"Grund `{record.get('reason')}`, {when}:\n\n"
                f"> {record.get('note')}\n\n"
                f"Hat sich das geändert, sag es hier im Issue. Dann nimmt ein Maintainer den "
                f"Eintrag aus `no-watch.json` heraus und setzt das Label neu.")
        dup, warum = already_watched(f["url"], f["osm_id"], args.entries)
        if dup and warum == "page":
            raise Refused(
                f"Diese Seite wird schon beobachtet: `entries/{dup}`.\n\n"
                f"Teilen sich zwei Betriebe die Seite, braucht der zweite einen eigenen "
                f"Schlüssel im Filter, meist seine Adresse (FILTERS.md Fall 12) — das ist eine "
                f"Änderung an der bestehenden Datei, kein neuer Watch.")
        if dup:
            raise Refused(
                f"Dieses OSM-Objekt wird schon beobachtet, über eine andere Seite: "
                f"`entries/{dup}`.\n\n"
                f"Ist die hier vorgeschlagene Seite die bessere, sag das im Issue: dann ändert "
                f"ein Maintainer die `url` in der vorhandenen Datei, statt einen zweiten Watch "
                f"auf denselben Betrieb zu legen.")
        notes = tag_note(f["tags"], args.entries)
        if due:
            notes = [f"ℹ Diese Seite stand seit {record.get('established')} auf der Sperrliste "
                     f"(`{record.get('reason')}`), war aber ab {record.get('recheck')} wieder "
                     f"anzusehen. Der Eintrag kommt beim Bauen aus `no-watch.json` heraus, im "
                     f"selben Pull Request. Was damals dort stand: {record.get('note')}"] + notes
        html, ranked = fetch(f["url"], f["lang"])
        if args.command == "candidates":
            write(args.out, "comment.md", candidates_comment(f, ranked, len(html), notes))
            write(args.out, "candidates.json", json.dumps(ranked, ensure_ascii=False, indent=1))
            return 0

        if not 1 <= pick <= min(len(ranked), MAX_CANDIDATES):
            raise Refused(f"`{pick}` stand nicht zur Wahl, gezeigt waren "
                          f"{min(len(ranked), MAX_CANDIDATES)} Kandidaten. Der Abruf von eben "
                          f"kann anders ausgefallen sein als der erste — dann setz das Label "
                          f"neu und such aus der neuen Liste.")
        cand = ranked[pick - 1]
        import filter_wizard as W
        path = W.emit_entry(os.path.join(args.out, "entry"), f["name"], f["url"], cand,
                            False, f["lang"], f["osm_id"], f["tags"])
        slug = os.path.basename(path)[:-len(".json")]
        if due:
            # A page may sit in exactly one of the two lists, so the record has to go in the
            # same commit — otherwise CI refuses the pull request the bot just built.
            write(args.out, "no-watch.json", drop_block(f["url"], args.absences))
        write(args.out, "pr-body.md", pr_body(f, cand, f"entries/{slug}.json", args.issue, notes))
        write(args.out, "meta.json", json.dumps(
            {"slug": slug, "name": f["name"], "url": f["url"], "pick": pick,
             "branch": f"watch/{slug}", "entry": path, "drops_block": bool(due),
             "title": f"watch: {f['name']}"}, ensure_ascii=False, indent=1))
        return 0
    except Refused as e:
        write(args.out, "comment.md",
              f"Das kann ich so nicht bauen.\n\n{e}\n\n"
              f"Ändere das Issue und setz das Label "
              f"`{'watch-weg' if args.drop else 'filter-fix' if args.fix else 'wizard'}` "
              f"neu, dann probiere ich es noch einmal.")
        print(f"refused: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
