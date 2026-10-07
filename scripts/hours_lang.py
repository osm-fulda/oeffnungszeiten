"""
hours_lang.py — language-aware detection of opening hours in page text.

Shared by filter_wizard.py and watch_audit.py. Stdlib only.

Why this exists: a naive time regex is wrong in German. `\\d{1,2}[:.]\\d{2}` misses
"10 - 18 Uhr" (no minutes at all) and matches *inside IP addresses*, so an anti-bot
block page reading "Sorry 212.110.223.68, your request cannot be processed" scores as
"hours found". Both mistakes produced wrong numbers in this project before being caught;
see FILTERS.md §3 Step 1.
"""
import hashlib
import re

# --------------------------------------------------------------------------- #
# Per-language patterns
# --------------------------------------------------------------------------- #
LANGS = {
    "de": {
        # headings that introduce an hours block, used to anchor XPaths
        "keywords": ["Öffnungszeiten", "ÖFFNUNGSZEITEN", "öffnungszeiten", "Öffnungszeit",
                     "ÖFFNUNGSZEIT", "Sprechzeiten", "SPRECHZEITEN", "Sprechstunde",
                     "Geschäftszeiten", "Servicezeiten", "Öffnung"],
        # substrings that mark an element's class/id as an hours container
        "class_hints": ["oeffnungszeit", "offnungszeit", "öffnungszeit", "sprechzeit",
                        "opening", "openinghours", "open-hours", "openhours", "hours",
                        "zeiten", "times", "opentime", "business-hours"],
        "days": [
            ("Mon", ["montag", "montags"], ["mo"]),
            ("Tue", ["dienstag", "dienstags"], ["di"]),
            ("Wed", ["mittwoch", "mittwochs"], ["mi"]),
            ("Thu", ["donnerstag", "donnerstags"], ["do"]),
            ("Fri", ["freitag", "freitags"], ["fr"]),
            ("Sat", ["samstag", "samstags", "sonnabend"], ["sa"]),
            ("Sun", ["sonntag", "sonntags"], ["so"]),
        ],
    },
    "en": {
        "keywords": ["Opening Hours", "Opening hours", "OPENING HOURS", "Opening Times",
                     "Opening times", "Business Hours", "Business hours", "Hours"],
        "class_hints": ["opening", "openinghours", "open-hours", "openhours", "hours",
                        "times", "opentime", "business-hours"],
        "days": [
            ("Mon", ["monday", "mondays"], ["mon"]),
            ("Tue", ["tuesday", "tuesdays"], ["tue", "tues"]),
            ("Wed", ["wednesday", "wednesdays"], ["wed"]),
            ("Thu", ["thursday", "thursdays"], ["thu", "thur", "thurs"]),
            ("Fri", ["friday", "fridays"], ["fri"]),
            ("Sat", ["saturday", "saturdays"], ["sat"]),
            ("Sun", ["sunday", "sundays"], ["sun"]),
        ],
    },
}

# 08:00 / 8.00 / "11 : 00" (some sites space out the colon) — but NOT inside an IP
# address, a decimal or a date, hence the lookarounds rejecting an adjacent digit or dot.
# "27.03.2025" is rejected because the lookahead sees the dot after "03".
# The optional seconds are for JSON-LD: `openingHoursSpecification` writes "opens": "09:00:00",
# and without them the lookahead tripped over the second colon — which made watch_audit report
# a working KIND watch as "no opening hours on this page at all".
# A dot after a letter is an abbreviation, not part of a number: "Mo.9.00 – 14.00 Uhr" is how the
# IKK writes its only hours, and rejecting every preceding dot made the wizard call that page blind.
_CLOCK = r'(?<![\d:])(?<!\d\.)\d{1,2}\s*[:.]\s*\d{2}(?::\d{2})?(?![\d.:])'
# "10 - 18 Uhr", "10–18 Uhr", "10 bis 18 Uhr", "18 Uhr"
_GERMAN_BARE = r'(?<!\d)\d{1,2}\s*(?:[-–—]|bis)\s*\d{1,2}\s*Uhr|(?<!\d)\d{1,2}(?:[.:]\d{2})?\s*Uhr'
# "Mo-Di 11-24h", "11 - 2 h" — German shorthand with an h suffix and no minutes
_GERMAN_H = r'(?<!\d)\d{1,2}\s*[-–—]\s*\d{1,2}\s*h\b'
# "Mo - Fr 8 - 18 | Sa 8 - 13" — a bare range with no unit at all. Only counted when a weekday
# stands directly in front of it, because "1 - 3" on its own is a price, a shoe size or a
# delivery estimate. Marien-Apotheke states its hours exactly once and exactly like this; without
# this shape the audit called a correct filter "no opening hours on this page at all".
_DAY_BARE = (r'\b(?:mo|di|mi|do|fr|sa|so)[a-zäöü]*\.?'
             r'(?:\s*(?:[-–—]|bis)\s*(?:mo|di|mi|do|fr|sa|so)[a-zäöü]*\.?)?'
             r'[\s:]*(?<![\d.:])\d{1,2}\s*[-–—]\s*\d{1,2}(?![\d.:])')
_ENGLISH_BARE = r'(?<!\d)\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)'
# "8°° - 12°° Uhr", "10.oo Uhr" — typewriter shapes for full hours. Café Wahl and Harry's
# Modellbau write nothing else, and the audit called both "no opening hours on this page".
_FULL_HOUR = r'(?<![\d.:])\d{1,2}\s*(?:°°|[.:]oo)(?![a-z\d])'

TIME_RE = re.compile(f'{_CLOCK}|{_GERMAN_BARE}|{_GERMAN_H}|{_DAY_BARE}|{_ENGLISH_BARE}|{_FULL_HOUR}',
                     re.I)

# Text shapes of an anti-bot / error interstitial. Phrases must be DISTINCTIVE: a bare
# "captcha" or "forbidden" also occurs in ordinary contact forms and legal text, and
# matching those marked a working shop page as blocked.
BLOCK_RE = re.compile(
    r'your request cannot be processed|request could not be processed'
    r'|access to this page has been denied|access denied\b|403 forbidden'
    r'|are you a human|verify (?:that )?you are (?:a )?human'
    r'|enable javascript and cookies to continue|checking your browser before'
    r'|attention required.{0,20}cloudflare|zugriff verweigert|bot detection'
    r'|rate limit exceeded|unusual traffic from your',
    re.I)
# A genuine interstitial replaces the page. If the text is long, the phrase is incidental.
BLOCK_MAX_CHARS = 2500


def looks_blocked(text):
    t = text or ""
    return bool(BLOCK_RE.search(t)) and len(t.strip()) <= BLOCK_MAX_CHARS


IPV4_RE = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b')

# Invisible characters that split words without being visible: soft hyphen, zero-width
# space/non-joiner/joiner, word joiner, BOM. German sites use soft hyphens for typesetting
# ("Frei­tag", "Sams­tag"), which silently hid five weekdays from La Gelateria's hours block.
INVISIBLE_RE = re.compile('[­​‌‍⁠﻿]')


def clean(text):
    """Strip invisible characters so word matching sees what a reader sees."""
    return INVISIBLE_RE.sub('', text or '')


def _lang(lang):
    if lang not in LANGS:
        raise SystemExit(f"unknown --lang {lang!r}; known: {', '.join(sorted(LANGS))}")
    return LANGS[lang]


def keywords(lang="de"):
    return _lang(lang)["keywords"]


def class_hints(lang="de"):
    return _lang(lang)["class_hints"]


def time_matches(text):
    """All time-like tokens, with their surrounding context, IPs removed first.

    Returns a list of (token, context) so a caller can PRINT what it matched before
    trusting it — the rule that would have caught the block-page false positives.
    """
    clean_txt = IPV4_RE.sub(" ", clean(text))
    out = []
    for m in TIME_RE.finditer(clean_txt):
        lo, hi = max(0, m.start() - 30), min(len(clean_txt), m.end() + 30)
        out.append((m.group(0), re.sub(r'\s+', ' ', clean_txt[lo:hi]).strip()))
    return out


def has_time(text):
    return bool(time_matches(text))


# German inflects it ("T\u00e4gliche \u00d6ffnungszeiten"), so match the stem \u2014 a trailing \b drops
# every form but the bare adverb.
EVERYDAY_RE = re.compile(
    r'\bt\u00e4glich|\bjeden\s+tag|\balle\s+tage\b|\bdaily\b|\bevery\s+day\b|\b7\s*tage\b',
    re.I)

ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# separator between two day names that means "everything in between"
_RANGE_SEP = re.compile(r'^[\s:.]*(?:[-–—]|bis|to|through)[\s:.]*$', re.I)


def _boundary_ok(text, start):
    """Is the match at `start` a real word start?

    Not as simple as \\b. Rendered pages concatenate nodes without whitespace, so a day
    name arrives glued to the previous word: "18:00 UhrSamstag10:00". A plain letter guard
    drops Samstag; a plain \\b drops "donnerstags8:00" (s and 8 are both word chars). So:
    accept a non-letter before it, OR a lower→upper CamelCase seam.

    A third seam: an ALL-CAPS heading glued to the day name, "ÖFFNUNGSZEITENMontag". That is
    upper→upper, so the CamelCase rule misses it — and missing "Montag" also loses the range
    it opens, so "Montag bis Freitag 09.00-18.00" was reported as 3 weekdays instead of 6
    (Salon Grünkorn). Accept it when the day name itself is Titlecase (upper followed by
    lower), which is what a node seam looks like; a day word buried inside one all-caps run
    ("ÖFFNUNGSZEITENMONTAG") still does not qualify.
    """
    if start == 0:
        return True
    prev = text[start - 1]
    if not prev.isalpha():
        return True
    if not text[start].isupper():
        return False
    if prev.islower():
        return True
    return len(text) > start + 1 and text[start + 1].islower()


def _day_hits(text, lang):
    """[(start, end, day_index)] for every weekday mentioned, left to right."""
    allow_abbrev = has_time(text)
    hits = []
    for idx, (_canon, full, abbrev) in enumerate(_lang(lang)["days"]):
        for w in full:
            for m in re.finditer(re.escape(w), text, re.I):
                if _boundary_ok(text, m.start()):
                    hits.append((m.start(), m.end(), idx))
        if allow_abbrev:
            for a in abbrev:
                for m in re.finditer(re.escape(a) + r'\.?(?![a-zäöüß])', text, re.I):
                    if _boundary_ok(text, m.start()):
                        hits.append((m.start(), m.end(), idx))
    # English full names always count as well, whatever the page language: a JSON-LD filter
    # renders as `https://schema.org/Monday`, so a German-only day list reported every
    # `json:$..openingHoursSpecification` watch as "times but no weekday named" — and acting
    # on that would have replaced good JSON-LD filters with nav menus and marketing text.
    # Full names only; English abbreviations are too collision-prone to add blindly.
    if lang != "en":
        for idx, (_canon, full, _abbrev) in enumerate(LANGS["en"]["days"]):
            for w in full:
                for m in re.finditer(re.escape(w), text, re.I):
                    if _boundary_ok(text, m.start()):
                        hits.append((m.start(), m.end(), idx))
    hits.sort()
    # drop abbreviation hits that sit inside a full-name hit ("Mo" within "Montag")
    out = []
    for h in hits:
        if out and h[0] < out[-1][1]:
            if h[1] - h[0] > out[-1][1] - out[-1][0]:
                out[-1] = h
            continue
        out.append(h)
    return out


def weekdays(text, lang="de"):
    """Distinct weekdays in `text`, as canonical Mon..Sun, with ranges expanded.

    "Mo - Fr" means five days, not two. Without expansion every normal German hours block
    looked like it captured only 2 weekdays, which produced a constant false "incomplete"
    warning. Abbreviations are ambiguous with ordinary words (German "so"), so they only
    count when the text also contains a time.
    """
    t = clean(text)
    # "täglich ab 18.00 Uhr" / "daily" covers the whole week without naming a single day.
    # Without this, perfectly complete hours blocks were reported as "no weekday named".
    if EVERYDAY_RE.search(t) and has_time(t):
        return list(ORDER)
    hits = _day_hits(t, lang)
    days = set()
    for i, (s, e, idx) in enumerate(hits):
        days.add(idx)
        if i + 1 < len(hits):
            s2, _e2, idx2 = hits[i + 1]
            if _RANGE_SEP.match(t[e:s2]):
                step = idx
                while step != idx2:
                    step = (step + 1) % 7
                    days.add(step)
    return [ORDER[i] for i in sorted(days)]


def weekdays_any(text, lang="de"):
    """Weekdays under the best-matching language.

    Abbreviations are language-specific ("Mo/Di/Mi" vs "MON/TUE/WED"), so a German-only pass
    reads none of Seize 16's "TUE - FRI: 10:30 AM". Full names are already counted in both
    languages; this covers the abbreviation case without making the caller guess.
    """
    best = weekdays(text, lang)
    for other in LANGS:
        if other == lang:
            continue
        alt = weekdays(text, other)
        if len(alt) > len(best):
            best = alt
    return best


def day_labels(lang="de"):
    """Canonical day -> short label in the page's language ('Mon' -> 'Mo' / 'Mon')."""
    return {canon: abbrev[0].capitalize()
            for canon, _full, abbrev in _lang(lang)["days"]}


def days_phrase(days, lang="de"):
    """Spell out which weekdays were found, for a human: 'Mo–Sa (6 of 7)'.

    The tools used to print the initials as one blob ('days:MTWTFS'), which nobody outside
    this repo can read — and the whole point of the wizard is that an admin decides by
    reading, not by decoding. Contiguous days collapse into a range, gaps stay visible
    ('Mo–Mi, Fr (4 of 7)') because a gap is exactly what tells you a sibling element holds
    the rest of the week.
    """
    if not days:
        return "no weekday named"
    lbl = day_labels(lang)
    idx = sorted({ORDER.index(d) for d in days if d in ORDER})
    runs = []
    for i in idx:
        if runs and i == runs[-1][1] + 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    parts = []
    for a, b in runs:
        if b == a:
            parts.append(lbl[ORDER[a]])
        elif b - a == 1:
            parts.append(f"{lbl[ORDER[a]]}, {lbl[ORDER[b]]}")
        else:
            parts.append(f"{lbl[ORDER[a]]}–{lbl[ORDER[b]]}")
    count = "all 7" if len(idx) == 7 else f"{len(idx)} of 7"
    return f"{', '.join(parts)} ({count})"


def hours_score(text, lang="de"):
    """How strongly `text` looks like a real opening-hours block. 0 = not at all."""
    if not text or not text.strip():
        return 0
    if looks_blocked(text):
        return 0
    times = time_matches(text)
    if not times:
        return 0
    return min(len(weekdays(text, lang)), 7) * 10 + min(len(times), 14)


def fingerprint(text):
    """Stable hash of normalised text — used to spot watches capturing identical
    content, which is how theme boilerplate ('Monday,…,Sunday 09:00-17:00' on four
    unrelated businesses) gets caught."""
    norm = re.sub(r'\s+', ' ', (text or "").strip().lower())
    return hashlib.md5(norm.encode("utf-8", "replace")).hexdigest()[:12]


def _folds(seq):
    """Every k for which `seq` is its own first 1/k repeated k times."""
    n = len(seq)
    if n < 2:
        return set()
    return {k for k in range(2, min(6, n // 2 + 1) + 1)
            if n % k == 0 and seq == seq[: n // k] * k}


def repeat_factor(text, lang="de"):
    """How many times the captured hours appear to be duplicated.

    Pages routinely ship a desktop AND a mobile copy of the same sidebar, and a selector
    that matches both captures everything twice. Harmless-looking, but it is how Davis got
    a guaranteed daily diff (today's line duplicated and re-ordered) and how the Mensa
    filter matched four copies. Returns 1 when there is no repetition.

    The **weekday sequence must repeat too**, not only the time tokens. Folding times alone
    calls every shop with uniform hours duplicated: C&A opens 09:30-19:00 Mon-Sat, so its
    twelve time tokens are trivially "two copies of six", and Aldi's 08:00-21:00 seven days
    a week is worse. That mislabelled 15 healthy watches as duplicate-capture while the two
    real cases (Prütz and Tomasu-san, whose filters genuinely match three elements) drowned
    in the noise. A real duplicate repeats the days as well: Mo Di Mi Do Fr Mo Di Mi Do Fr.
    """
    t = clean(text)
    toks = [tok for tok, _ in time_matches(t)]
    if len(toks) < 4:
        return 1
    time_folds = _folds(toks)
    if not time_folds:
        return 1
    # Pick the language that recognises the most day names, as weekdays_any does — an English
    # page must not read as "no weekdays" and thereby escape the check.
    hits = max((_day_hits(t, lg) for lg in LANGS), key=len)
    if not hits:
        # No weekday named at all: a repeated time list is then indistinguishable from a
        # legitimate list of many entries (Emaillierwerk's 165 tenant rows). Say nothing.
        return 1
    common = time_folds & _folds([idx for _s, _e, idx in hits])
    return max(common) if common else 1


CLOSED_RE = re.compile(r'geschlossen|closed|ruhetag', re.I)
# Words that may stand beside a bare time without making it a labelled statement.
LEAD_NOISE = re.compile(r'\buhr\b|\bo\'?clock\b|\bhrs?\b', re.I)


def bare_lead(text, lang="de"):
    r"""Does an unlabelled time or `Geschlossen` sit before the first weekday?

    The shape of a "today" widget rendered into the same capture as the weekly table: the page
    writes the current day's value with no day beside it, so the text changes whenever that
    day's hours differ. It is invisible on a Tuesday and fires on the Sunday.

    Only a *bare* value counts. "Täglich von 17:00 bis 00:00 Uhr" before the table is a
    statement about the week and stays unflagged — otherwise the check would push correct
    captures down the ranking.

    >>> bare_lead(": 9:30 - 19:00Mo.9:30 - 19:00Di.9:30 - 19:00")
    True
    >>> bare_lead("Geschlossen Montag 9-17 Dienstag 9-17")
    True
    >>> bare_lead("Täglich von 17:00 bis 00:00 Uhr Samstags open end")
    False
    >>> bare_lead("Öffnungszeiten Montag 9-17")
    False
    >>> bare_lead("Montag 9-17 Dienstag 9-17")
    False
    """
    hits = _day_hits(text, lang)
    if not hits:
        return False
    lead = text[:hits[0][0]]
    if not (TIME_RE.search(lead) or CLOSED_RE.search(lead)):
        return False
    rest = TIME_RE.sub(" ", lead)
    rest = CLOSED_RE.sub(" ", rest)
    rest = LEAD_NOISE.sub(" ", rest)
    return not re.search(r'[^\W\d_]', rest)


def uniform_hours(text, lang="de"):
    """True when the text lists ~every day INDIVIDUALLY with one identical time range —
    the shape of generated theme boilerplate ("Monday 09:00-17:00 Tuesday 09:00-17:00 …").

    Counts explicit day mentions, not range-expanded ones: a normal "Mo - Fr 08:00-18:00"
    is uniform by definition and must not be flagged. Still only a WARNING — a few real
    businesses do open the same hours all week.
    """
    if len(_day_hits(text, lang)) < 6:
        return False
    days = weekdays(text, lang)
    if len(days) < 5:
        return False
    # Compare one range PER DAY. Splitting on newlines alone is not enough: rendered text
    # often arrives as a single line ("Montag–Donnerstag: 14–22 UhrFreitag & Samstag: 11–0
    # Uhr…"), which collapses to one chunk and looked uniform when it plainly is not.
    hits = _day_hits(text, lang)
    chunks = [text[s:hits[i + 1][0]] if i + 1 < len(hits) else text[s:]
              for i, (s, _e, _idx) in enumerate(hits)]
    ranges = set()
    for chunk in chunks:
        toks = [t for t, _ in time_matches(chunk)]
        if len(toks) >= 2:
            ranges.add(f"{toks[0]}-{toks[1]}")
    # One distinct range across six or more individually named days is boilerplate, however
    # few chunks carried a time: the classic offender is a single line reading
    # "Monday,Tuesday,…,Sunday 09:00-17:00", which has seven day names and exactly one range.
    # Exactly one — not "at most one". An empty set means no range could be parsed, which is
    # ignorance, not evidence of boilerplate, and returning True there flagged per-day hours
    # written as "Montag 8-12 Dienstag 9-13" (no Uhr, no minutes, so no range parsed).
    return len(ranges) == 1
