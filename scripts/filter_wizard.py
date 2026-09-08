#!/usr/bin/env python3
"""
filter_wizard.py — pick the right opening-hours filter for one page, without devtools.

The point: an admin can judge TEXT ("Mo-Fr 8:00-18:00") but not a SELECTOR
("div.elementor-element-224ed87 > p:nth-child(3)"). So this never asks you to choose a
selector. It runs every strategy from FILTERS.md, then prints the *text each candidate
would capture* as a numbered menu. You read German (or English) and type a number.

Strategies tried, best-first (FILTERS.md §2):
  1. JSON-LD          — schema.org openingHours / openingHoursSpecification
  2. heading-anchored — smallest element under an "Öffnungszeiten" heading with a time
  3. class/id         — containers whose authored class or id names hours
  4. day-anchored text — hours with NO heading and no helpful class ("Mo-Sa 10 - 18 Uhr" in a
                        bare <p>): find the innermost element that looks like hours and anchor
                        on its nearest durable ancestor, or on the weekday word itself
  5. common ancestor  — when the week is split across siblings ("Dienstag-Freitag" in one <p>,
                        "Samstag" in the next), anchor their lowest common ancestor
  6. whole page       — always offered last, so "no filter" stays a visible choice

Examples:
  python3 scripts/filter_wizard.py https://example.de/kontakt
  python3 scripts/filter_wizard.py --uuid 8f2a1c3d…            # existing watch, URL from CD
  python3 scripts/filter_wizard.py https://example.de --render # JS-rendered page
  python3 scripts/filter_wizard.py --uuid 8f2a… --best --apply # non-interactive, take rank 1
  python3 scripts/filter_wizard.py https://example.de --json   # machine-readable, no prompt

Nothing is written unless you pick a candidate and --apply (or answer the prompt).
"""
import argparse
import json
import os
import re
import sys
import urllib.request

import lxml.html

import hours_lang as L
import osm_cd_common as C

HEAD_TAGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'p', 'span', 'div', 'li', 'td', 'th']
MAX_CAPTURE = 4000          # a filter capturing more than this is not a filter
SAFE_CLASS = re.compile(r'^[A-Za-z][\w-]{2,40}$')
# Wider than SAFE_CLASS: anything that survives being pasted into an XPath string
# literal. A generated name is a bad anchor, not an unusable one.
XPATH_SAFE = re.compile(r'^[\w.:-]{2,60}$')
# Page-builder / generated names: they change on the next site edit (FILTERS.md §3 Step 2).
# Not just hex — Beaver Builder emits base36 ids like "fl-icon-text-cjg0i7ku1qhr", so also
# flag any long token mixing letters and digits with no separator.
BRITTLE = re.compile(
    r'[0-9a-f]{6,}'
    r'|elementor-element-|fl-(?:node|icon|module|rich)-|et_pb_\w*_\d|wp-block-[0-9]'
    r'|css-[a-z0-9]{5,}|sc-[a-zA-Z0-9]{6,}|uagb-|kt-\w*[0-9a-z]{6,}|hype-obj-'
    # random-looking token, either case: 8+ chars mixing letters and digits with no separator.
    # Tumult Hype emits UPPERCASE ids like "hype-obj-FQKA9M3088D50NH7XXPN", which a
    # lowercase-only pattern rated durable — the resulting filter matched nothing at all.
    r'|(?:^|[-_])(?=[A-Za-z0-9]{8,}(?:$|[-_]))(?=[A-Za-z]*\d)(?=[0-9]*[A-Za-z])[A-Za-z0-9]{8,}')


def txt_of(el):
    return re.sub(r'\s+', ' ', el.text_content()).strip()


def fetch_plain(url):
    req = urllib.request.Request(C.normalize_url(url), headers=C.UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        enc = r.headers.get_content_charset() or 'utf-8'
        # 900 KB truncated real pages mid-document and cut off footers where hours often
        # live (mylunico.de, ninawenzel-training.de both exceeded it), which looked exactly
        # like "this page publishes no hours".
        return r.read(5_000_000).decode(enc, 'replace')


def fetch_rendered(url, browser_ws=None):
    """Render the page through sockpuppetbrowser, talking CDP to it directly.

    `--browser-ws` names the browser; without it a browser on the default port is used, since
    that case needs no flag. Either way nothing is executed inside changedetection: a plain
    `docker run -p 3000:3000 dgtlmoon/sockpuppetbrowser` is the whole requirement, and CI can
    do the same. Say which browser was taken: a silent choice of renderer is how you end up
    debugging a filter against HTML you did not think you were looking at.
    """
    import cdp_render

    if browser_ws:
        return cdp_render.render(url, ws_url=browser_ws)
    product = cdp_render.probe(cdp_render.DEFAULT_WS, timeout=3)
    if not product:
        raise RuntimeError(
            "no browser to render with. Start one and it is found without a flag:\n"
            "    docker run --rm -p 3000:3000 dgtlmoon/sockpuppetbrowser\n"
            "  or name another with --browser-ws ws://host:3000. Use a throwaway browser, not "
            "the cluster's: that one serves every html_webdriver watch while you render."
        )
    print(f"rendering via {cdp_render.DEFAULT_WS} ({product})")
    return cdp_render.render(url, ws_url=cdp_render.DEFAULT_WS)


# --------------------------------------------------------------------------- #
# Strategy 1 — JSON-LD
# --------------------------------------------------------------------------- #
def _walk(node, key_lc):
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() == key_lc:
                yield v
            yield from _walk(v, key_lc)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key_lc)


def _flatten_spec(val):
    """openingHoursSpecification -> readable lines; openingHours -> as-is."""
    lines = []
    items = val if isinstance(val, list) else [val]
    for it in items:
        if isinstance(it, str):
            lines.append(it)
        elif isinstance(it, dict):
            day = it.get('dayOfWeek') or it.get('dayofweek') or ''
            if isinstance(day, list):
                day = ', '.join(d.rsplit('/', 1)[-1] for d in day if isinstance(d, str))
            elif isinstance(day, str):
                day = day.rsplit('/', 1)[-1]
            o, c = it.get('opens', ''), it.get('closes', '')
            lines.append(f"{day} {o}-{c}".strip())
    return lines


def jsonld_candidates(html):
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for key, sel in (('openinghoursspecification', 'json:$..openingHoursSpecification'),
                         ('openinghours', 'json:$..openingHours')):
            lines = []
            for v in _walk(data, key):
                lines += _flatten_spec(v)
            if lines:
                out.append({'strategy': 'json-ld', 'filter': sel,
                            'text': ' · '.join(dict.fromkeys(lines))})
    return out


# --------------------------------------------------------------------------- #
# Strategy 2 — heading-anchored XPath
# --------------------------------------------------------------------------- #
def heading_candidates(doc, lang):
    out = []
    for kw in L.keywords(lang):
        for tag in HEAD_TAGS:
            xp_base = f'//{tag}[contains(normalize-space(.),"{kw}")]'
            try:
                els = doc.xpath(xp_base)
            except Exception:
                continue
            if not els:
                continue
            for rel, getter in (('', lambda e: e),
                                ('/parent::*', lambda e: e.getparent()),
                                ('/parent::*/parent::*',
                                 lambda e: e.getparent().getparent() if e.getparent() is not None else None)):
                xp = f'({xp_base}){rel}'
                try:
                    sel = doc.xpath(xp)
                except Exception:
                    continue
                if not sel:
                    continue
                text = ' '.join(txt_of(x) for x in sel if x is not None).strip()
                if not text or len(text) > MAX_CAPTURE:
                    continue
                out.append({'strategy': f'heading "{kw}"', 'filter': 'xpath:' + xp, 'text': text})
    return out


# --------------------------------------------------------------------------- #
# Strategy 2b — heading and hours are separate sibling blocks
# --------------------------------------------------------------------------- #
def _durable_xpath(el):
    """An XPath naming this one element by an authored id or class, or None."""
    eid = (el.get('id') or '').strip()
    if eid and SAFE_CLASS.match(eid) and not BRITTLE.search(eid):
        return f'//*[@id="{eid}"]'
    cls = _clean_class(el)
    if cls:
        return f'//{el.tag}[contains(concat(" ",normalize-space(@class)," ")," {cls} ")]'
    return None


def _resolves_to(doc, xp, text):
    """True when `xp` picks exactly one element and it is the one we meant."""
    try:
        sel = doc.xpath(xp)
    except Exception:
        return False
    return len(sel) == 1 and txt_of(sel[0]) == text


def heading_sibling_candidates(doc, lang):
    """Strategy 2 finds the word and misses the times, because they are not in the same box.

    Page builders give every section its own row: Krieger Schrott (Duda) has the
    "Öffnungszeiten" heading in one `dmRespRow` and "Mo. - Fr. 08:00 - 15:30" in the next, so
    the heading, its parent and its grandparent all capture the heading and nothing else — and
    the page reads as if it published no hours at all. So walk **up** from the heading and take
    the first ancestor whose next sibling carries hours, anchored on that ancestor.

    Only for headings that carry no hours themselves. Where strategy 2 already works, guessing
    forward would add a second candidate for the same page and no information.
    """
    out, seen = [], set()
    page_len = len(txt_of(doc)) or 1
    for kw in L.keywords(lang):
        for tag in HEAD_TAGS:
            try:
                els = doc.xpath(f'//{tag}[contains(normalize-space(.),"{kw}")]')
            except Exception:
                continue
            for el in els:
                own = txt_of(el)
                # a heading is short: without this the walk starts at <body> as well and every
                # section on the page becomes a candidate
                if len(own) > len(kw) + 60 or L.hours_score(own, lang) > 0:
                    continue
                node, hops = el, 0
                while node is not None and hops < 5:
                    # Only climb through boxes that hold the heading and nothing else. Without
                    # this the walk starts at a nav link, reaches a page-level container five
                    # hops up and captures whatever section follows it: measured on
                    # deluxe-barbier and praxis-contour, where it outranked the right filter.
                    if len(txt_of(node)) > len(kw) + 60:
                        break
                    sib = node.getnext()
                    while sib is not None and not isinstance(sib.tag, str):
                        sib = sib.getnext()
                    if sib is not None:
                        text = txt_of(sib)
                        # An hours block is small. A menu entry also passes the heading test
                        # (deluxebarbier.de has "Öffnungszeiten" in its nav, 71 characters of
                        # menu), and the sibling of a nav is the whole <main> — which holds
                        # hours somewhere and would outrank the right filter.
                        wide = len(text) > 0.4 * page_len and len(text) > 600
                        if (text and len(text) <= MAX_CAPTURE and not wide
                                and L.hours_score(text, lang) > 0):
                            anchor = _durable_xpath(node)
                            xp = anchor and anchor + '/following-sibling::*[1]'
                            if xp and xp not in seen and _resolves_to(doc, xp, text):
                                seen.add(xp)
                                out.append({'strategy': f'heading "{kw}" → next block',
                                            'filter': 'xpath:' + xp, 'text': text})
                                break
                    node, hops = node.getparent(), hops + 1
    return out


# --------------------------------------------------------------------------- #
# Strategy 3 — authored class / id container
# --------------------------------------------------------------------------- #
def class_candidates(doc, lang):
    hints = L.class_hints(lang)
    seen, out = set(), []
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ('class', 'id'):
            raw = el.get(attr) or ''
            for token in raw.split():
                tl = token.lower()
                if not any(h in tl for h in hints) or not SAFE_CLASS.match(token):
                    continue
                key = (attr, token)
                if key in seen:
                    continue
                seen.add(key)
                if attr == 'id':
                    xp = f'//*[@id="{token}"]'
                else:
                    xp = f'//*[contains(concat(" ",normalize-space(@class)," ")," {token} ")]'
                try:
                    sel = doc.xpath(xp)
                except Exception:
                    continue
                text = ' '.join(txt_of(x) for x in sel).strip()
                if not text or len(text) > MAX_CAPTURE:
                    continue
                out.append({'strategy': f'{attr}="{token}"', 'filter': 'xpath:' + xp, 'text': text})
    return out


# --------------------------------------------------------------------------- #
# Strategy 4 — content-anchored: hours with no heading and no helpful class
# --------------------------------------------------------------------------- #
# Plenty of pages print "Kaffeeladen im Steinweg: Mo-Sa 10 - 18 Uhr" with no
# "Öffnungszeiten" anywhere and no meaningful class. Strategies 2 and 3 are both blind to
# those, which is most of the remaining backlog. So: find the *smallest* elements whose own
# text looks like hours, then anchor on something durable about them.
CONTAINERS = {'p', 'div', 'li', 'td', 'section', 'article', 'ul', 'ol', 'table', 'tbody',
              'span', 'dl', 'address', 'h2', 'h3', 'h4'}


# grid/utility classes carry no meaning and appear hundreds of times per page
LAYOUT_CLASS = re.compile(
    r'^(col|row|container|wrapper|inner|outer|clearfix|d-flex|flex|grid|block|item|content|'
    r'left|right|center|main|wide|full|half|hidden|visible|active|first|last|'
    r'[mp][trblxy]?-\d|w-\d|h-\d|text-\w+|bg-\w+|js-\w+)', re.I)


def _clean_class(el):
    for token in (el.get('class') or '').split():
        if (SAFE_CLASS.match(token) and not BRITTLE.search(token)
                and not LAYOUT_CLASS.match(token) and len(token) >= 5):
            return token
    return None


_LOWER = 'translate(.,"MONTAGDIESWCHRFPÄÖÜB","montagdieswchrfpäöüb")'


def _innermost_text_xpath(tag, word):
    """Elements containing `word` that have no descendant of the same tag containing it.

    Without the not(...) clause every ancestor matches too and the filter swallows the
    page. This form needs no class or id at all, which is what page-builder markup leaves
    us with.
    """
    test = f'contains({_LOWER},"{word}")'
    return f'//{tag}[{test}][not(.//{tag}[{test}])]'


def _capture_len(el, xp):
    """Length of the text `xp` would capture, or None if it does not resolve."""
    try:
        sel = el.getroottree().xpath(xp)
    except Exception:
        return None
    if not sel:
        return None
    return len(' '.join(txt_of(x) for x in sel).strip())


def _anchor_xpath(el, lang):
    """A durable XPath for this element, best-first, each candidate VALIDATED against what
    it actually captures — an ancestor class like `fl-module` matches dozens of unrelated
    blocks and would silently turn a 71-character filter into the whole page."""
    own = len(txt_of(el)) or 1
    tries, weak = [], []
    eid = (el.get('id') or '').strip()
    if eid and XPATH_SAFE.match(eid):
        (tries if SAFE_CLASS.match(eid) and not BRITTLE.search(eid) else weak).append(
            f'//*[@id="{eid}"]')
    cls = _clean_class(el)
    if cls:
        tries.append(f'//{el.tag}[contains(concat(" ",normalize-space(@class)," ")," {cls} ")]')
    else:
        for token in (el.get('class') or '').split():
            if XPATH_SAFE.match(token) and len(token) >= 5 and not LAYOUT_CLASS.match(token):
                weak.append(f'//{el.tag}[contains(concat(" ",normalize-space(@class)," ")'
                            f'," {token} ")]')
                break
    node, hops = el.getparent(), 0
    while node is not None and hops < 3:
        nid = (node.get('id') or '').strip()
        if nid and SAFE_CLASS.match(nid) and not BRITTLE.search(nid):
            tries.append(f'//*[@id="{nid}"]')
        ncls = _clean_class(node)
        if ncls:
            tries.append(
                f'//{node.tag}[contains(concat(" ",normalize-space(@class)," ")," {ncls} ")]')
        node, hops = node.getparent(), hops + 1
    text = txt_of(el)
    for _canon, full, _ab in L.LANGS[lang]["days"]:
        for word in full:
            if re.search(re.escape(word), text, re.I):
                tries.append(_innermost_text_xpath(el.tag, word.lower()))
                break
    ok = []
    for rank, xp in enumerate(tries):
        got = _capture_len(el, xp)
        # reject anchors that balloon: they are matching unrelated siblings
        if got is not None and got <= max(3 * own, own + 400):
            ok.append((got, rank, xp))
    if not ok:
        # Every durable anchor was either rejected as generated or ballooned into unrelated
        # siblings, and the element still holds the hours. Offer the generated one: `score()`
        # flags it "avoid — brittle selector", but the captured text is on screen and the page
        # stops reading as "publishes no hours at all". That false answer is what turned away
        # Krieger Schrott, whose Duda markup names every box `u_1535202874`.
        for xp in weak:
            got = _capture_len(el, xp)
            if got is not None and got <= max(3 * own, own + 400):
                return [xp]
        return []
    # Tightest capture wins; among near-equals (within 20%) keep the more durable one,
    # i.e. the earlier entry — id before class before ancestor class before text anchor.
    best = min(g for g, _r, _x in ok)
    near = [(r, x) for g, r, x in ok if g <= best * 1.2 + 10]
    near.sort()
    seen, out = set(), []
    for _r, xp in near + [(r, x) for _g, r, x in sorted(ok)]:
        if xp not in seen:
            seen.add(xp)
            out.append(xp)
    return out[:2]


def _lca(els):
    """Lowest common ancestor of a list of elements."""
    if len(els) < 2:
        return None
    chain = [els[0]] + list(els[0].iterancestors())
    for node in chain:
        if all(e is node or node in list(e.iterancestors()) for e in els[1:]):
            return node
    return None


def combined_candidate(doc, lang, innermost):
    """One element covering ALL the hours on the page.

    Sites often split the week across sibling elements — "Dienstag – Freitag …" in one
    <p>, "Samstag …" in the next. Each single element is an incomplete filter, and the
    only complete non-brittle option is their common ancestor.
    """
    node = _lca(innermost)
    if node is None:
        return []
    for xp in _anchor_xpath(node, lang):
        try:
            sel = doc.xpath(xp)
        except Exception:
            continue
        text = ' '.join(txt_of(x) for x in sel).strip()
        if text and len(text) <= MAX_CAPTURE:
            return [{'strategy': 'common ancestor', 'filter': 'xpath:' + xp, 'text': text}]
    return []


def content_candidates(doc, lang):
    out, seen = [], set()
    innermost = []
    for el in doc.iter():
        if not isinstance(el.tag, str) or el.tag not in CONTAINERS:
            continue
        text = txt_of(el)
        if not text or len(text) > 1200:
            continue
        if L.hours_score(text, lang) <= 0:
            continue
        # keep only the innermost such element: if a child already covers these hours,
        # the parent adds nothing but noise
        if any(isinstance(c.tag, str) and L.hours_score(txt_of(c), lang) >= L.hours_score(text, lang)
               for c in el):
            continue
        innermost.append(el)
        for xp in _anchor_xpath(el, lang):
            if xp in seen:
                continue
            seen.add(xp)
            try:
                sel = doc.xpath(xp)
            except Exception:
                continue
            got = ' '.join(txt_of(x) for x in sel).strip()
            if not got or len(got) > MAX_CAPTURE:
                continue
            out.append({'strategy': 'day-anchored text', 'filter': 'xpath:' + xp, 'text': got})
        if len(out) >= 40:
            break
    return out + combined_candidate(doc, lang, innermost[:8])


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
BASE = {'json-ld': 100, 'day-anchored text': 60, 'common ancestor': 75}


def score(cand, page_len, lang):
    text = cand['text']
    base = BASE.get(cand['strategy'], 70 if cand['strategy'].startswith('heading') else 65)
    if cand['strategy'] == 'whole page':
        base = 0
    s = base + L.hours_score(text, lang)
    s -= len(text) // 150
    # Each warning carries a severity so verdict() can say whether it is disqualifying.
    # 3+ = do not use this candidate, 2 = works but has a known cost, 1 = cosmetic.
    flags, sev = [], []
    def flag(text_, severity):
        flags.append(text_)
        sev.append(severity)
    if BRITTLE.search(cand['filter']):
        s -= 40
        flag('brittle selector (generated class — dies on next site edit)', 3)
    if page_len and len(text) > 0.7 * page_len and cand['strategy'] != 'whole page':
        s -= 30
        flag('wide (>70% of the page)', 2)
    days = L.weekdays(text, lang)
    if not days:
        s -= 25
        flag('no weekday named', 3)
    elif len(days) < 3:
        s -= 10
        flag(f'only {len(days)} weekday(s) — rest may be in a sibling element', 2)
    if L.uniform_hours(text, lang):
        s -= 15
        flag('same hours every day — check this is real, not theme boilerplate', 2)
    if L.bare_lead(text, lang):
        s -= 20
        flag('an unlabelled time sits before the weekday table — usually a "today" widget '
             'in the same wrapper, which rewrites itself daily', 2)
    rep = L.repeat_factor(text)
    if rep > 1:
        s -= 20
        flag(f'captures the same hours {rep}× (page ships duplicate copies) — '
             f'prefer a narrower element', 2)
    if L.looks_blocked(text):
        s -= 100
        flag('looks like a block/captcha page, not content', 4)
    if re.search(r'@|\+49|Telefon|Tel\.', text) and cand['strategy'] != 'whole page':
        flag('also captures contact details (harmless but noisier)', 1)
    cand['flags'] = flags
    cand['severity'] = max(sev) if sev else 0
    cand['worst'] = flags[sev.index(cand['severity'])] if sev else ''
    cand['days'] = days
    return s


def verdict(cand):
    """One plain-language line: is this candidate usable, and if not, what disqualifies it.

    The `!` warnings state facts; a reader still has to know which of them is fatal
    ('brittle selector' is, 'also captures contact details' is not). Ranking alone does not
    say that either — a page whose only candidates are all bad still produces a rank 1.
    """
    if cand['strategy'] == 'whole page':
        return 'last resort — no filter, so any banner or teaser on the page alerts'
    sev = cand.get('severity', 0)
    # With a single warning the reason is already on screen; naming it again just doubles
    # the line. With several, say which one drove the verdict.
    worst = (cand.get('worst') or '') if len(cand.get('flags') or []) > 1 \
        else 'see the warning above'
    if sev >= 3:
        return f'avoid — {worst}'
    if sev == 2:
        return f'usable, but — {worst}'
    if sev == 1:
        return ('good pick — the warning above is cosmetic'
                if worst == 'see the warning above' else f'good pick — only caveat: {worst}')
    return 'good pick — no warnings'


def _blocked(html):
    """Does this response body read as a block page rather than as the site?"""
    try:
        return L.looks_blocked(txt_of(strip_noise(lxml.html.fromstring(html))))
    except Exception:
        return False


def strip_noise(doc):
    """Drop script/style/noscript before reading text.

    changedetection does the same, and it matters: inline CSS like
    `rgba(190,159,85,0.25)` otherwise reads as a time token.
    """
    for bad in doc.xpath('//script|//style|//noscript|//template'):
        bad.getparent().remove(bad)
    return doc


def capture(html, filt):
    """What an existing changedetection filter grabs from this page, or None if we cannot say.

    Only the two forms the entries use are honoured. `json:` filters read a JSON-LD block rather
    than the DOM, and a bare CSS selector needs cssselect, which the wizard does not install —
    both return None so the caller can say "nicht ausgewertet" instead of showing a wrong text.

    >>> capture('<div class="h"><p>Mo 9-17</p></div>',
    ...         'xpath://*[contains(concat(" ",normalize-space(@class)," ")," h ")]')
    'Mo 9-17'
    >>> capture('<p>x</p>', 'json:$..openingHours') is None
    True
    """
    if not filt:
        return None
    body = filt.split(":", 1)[1] if filt.startswith(("xpath:", "xpath1:")) else None
    if body is None:
        return None
    try:
        doc = lxml.html.fromstring(html)
        found = doc.xpath(body)
    except Exception:
        return None
    if not found:
        return ""
    if filt.startswith("xpath1:"):
        found = found[:1]
    return " ".join(txt_of(el) if hasattr(el, "text_content") else str(el).strip()
                    for el in found).strip()


def collect(html, lang, page_text_len=None):
    doc = strip_noise(lxml.html.fromstring(html))
    page_len = page_text_len if page_text_len is not None else len(txt_of(doc))
    cands = (jsonld_candidates(html) + heading_candidates(doc, lang)
             + heading_sibling_candidates(doc, lang)
             + class_candidates(doc, lang) + content_candidates(doc, lang))
    # keep only things that actually look like hours; the whole page is added separately
    cands = [c for c in cands if L.hours_score(c['text'], lang) > 0]
    # de-duplicate on captured text — many selectors resolve to the same block
    best = {}
    for c in cands:
        c['score'] = score(c, page_len, lang)
        fp = L.fingerprint(c['text'])
        if fp not in best or c['score'] > best[fp]['score']:
            best[fp] = c
    ranked = sorted(best.values(), key=lambda c: -c['score'])
    whole = {'strategy': 'whole page', 'filter': '', 'text': txt_of(doc)}
    whole['score'] = score(whole, page_len, lang)
    whole['flags'] = ['no filter — every change anywhere on the page will alert'] + whole['flags']
    ranked.append(whole)
    return ranked


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #
def preview(text, width=150):
    t = re.sub(r'\s+', ' ', text).strip()
    return t[:width] + ('…' if len(t) > width else '')


def show(ranked, lang):
    print()
    print("Sorted best-first, but [1] is a guess, not a verdict: only you can tell whether")
    print("the text below is THIS business's hours. Read the text, ignore the selector.")
    print("  !  what the tool noticed        →  whether that disqualifies the candidate")
    print()
    for i, c in enumerate(ranked, 1):
        print(f"[{i}] {c['strategy']:<34} {L.days_phrase(c.get('days'), lang):<22} "
              f"{len(c['text']):>5} chars")
        print(f"    {preview(c['text'])}")
        for f in c['flags']:
            print(f"    ! {f}")
        print(f"    → {verdict(c)}")
        print(f"    filter: {c['filter'] or '(none)'}")
        print()


def recipe(url, chosen, rendered, idx, uuid):
    """Exactly what to type into the changedetection UI. This is the whole deliverable for
    someone who just has a URL and wants a watch — no datastore, no scripts, no API."""
    backend = ("Chrome/Javascript  (Playwright — the hours are NOT in the plain HTML)"
               if rendered else "Basic fast text  (plain fetch — no browser needed)")
    print("\n" + "─" * 72)
    print("Add this in changedetection  (Edit watch → these fields)")
    print("─" * 72)
    print(f"  URL                        {url}")
    print(f"  Fetch method               {backend}")
    print(f"  Filters & Triggers →       {chosen['filter'] or '(leave empty — whole page)'}"
          f"\n    CSS/JSONPath/XPath Filter")
    if L.repeat_factor(chosen['text']) > 1:
        print("  Also tick 'sort text alphabetically' — the block repeats and may reorder")
    print("\n  It should then capture exactly this:")
    for line in re.sub(r'\s{2,}', ' ', chosen['text']).strip().split('\n')[:6]:
        print(f"    {line[:100]}")
    for f in chosen['flags']:
        print(f"  ! {f}")
    print("─" * 72)
    if uuid:
        print("Apply it for me instead:")
        print(f"  python3 scripts/filter_wizard.py --uuid {uuid} --pick {idx} --apply"
              + (" --render" if rendered else ""))


def emit_entry(outdir, name, url, chosen, rendered, lang, osm_id=None, tags=None):
    """Write the entry file the PR workflow expects (CONCEPT.md)."""
    import cd_export
    entry = {"schema": 1, "name": name, "url": url, "lang": lang,
             "added": C.today()}
    # Tag NAMES, never uuids — a tag uuid means nothing in another instance (cd_export.py).
    if tags:
        entry["tags"] = sorted({t.strip() for part in tags for t in part.split(",") if t.strip()})
    if chosen["filter"]:
        entry["filter"] = chosen["filter"]
    if rendered:
        entry["fetch_backend"] = "html_webdriver"
    if L.repeat_factor(chosen["text"]) > 1:
        entry["sort_text_alphabetically"] = True
    if osm_id:
        entry["osm_id"] = osm_id
    # what a reviewer reads in the diff instead of fetching the page themselves
    entry["captured_sample"] = " ".join(chosen["text"].split())[:200]
    slug = cd_export.slugify(name, url)
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, slug + ".json")
    with open(path, "w") as fh:
        json.dump(entry, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    return path


def main():
    ap = argparse.ArgumentParser(description="Pick an opening-hours filter for a page")
    ap.add_argument("url", nargs="?", help="page URL (omit if --uuid given)")
    ap.add_argument("--uuid", help="changedetection watch to read the URL from / apply to")
    ap.add_argument("--lang", default="de", help="page language for keywords (de, en)")
    ap.add_argument("--render", action="store_true",
                    help="render through a browser first (see --browser-ws)")
    ap.add_argument("--no-render", action="store_true",
                    help="never fall back to the browser, even if the plain fetch finds nothing")
    ap.add_argument("--best", action="store_true", help="take rank 1 without prompting")
    ap.add_argument("--pick", type=int, help="take this rank without prompting")
    ap.add_argument("--apply", action="store_true", help="write the chosen filter to the watch")
    ap.add_argument("--emit", metavar="DIR", nargs="?", const="entries",
                    help="write an entry file for the PR workflow (default dir: entries/)")
    ap.add_argument("--name", help="business name for the entry file (default: watch title/host)")
    ap.add_argument("--osm-id", help="optional OSM id to record in the entry")
    ap.add_argument("--tags", action="append", default=[], metavar="TAG",
                    help="category tag for the entry (e.g. fulda-hairdresser). Repeatable, or "
                         "comma-separated. Without one the watch loses its grouping — and the "
                         "notification setup is copied from a sibling with the same tag.")
    ap.add_argument("--json", action="store_true", help="print candidates as JSON, no prompt")
    ap.add_argument("--base-url",
                    default=os.environ.get("CD_BASE_URL", "http://localhost:5000"),
                    help="changedetection API root; defaults to $CD_BASE_URL, so the same call "
                         "works in-cluster, on the VPS and through a tunnel "
                         "(see scripts/cd_env.sh)")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--container", default="changedetection",
                    help="container name (docker) or pod/deployment (kubectl)")
    ap.add_argument("--browser-ws", default=os.environ.get("BROWSER_WS"),
                    metavar="WS_URL",
                    help="render by talking CDP to this browser, e.g. ws://localhost:3000 after "
                         "`docker run --rm -p 3000:3000 dgtlmoon/sockpuppetbrowser`. Without it "
                         "a browser on that default port is used if one answers.")
    args = ap.parse_args()

    api = None
    url = args.url
    if args.uuid or args.apply:
        api = C.CDIO(args.base_url, C.resolve_api_key(args.api_key, args.container))
    if args.uuid:
        w = api.get(args.uuid)
        url = url or w.get("url")
        cur = [f for f in (w.get("include_filters") or []) if f.strip()]
        print(f"watch  : {w.get('title') or '(untitled)'}")
        print(f"current: {cur or '(no filter — whole page)'}")
    if not url:
        sys.exit("ERROR: give a URL or --uuid")

    # Punycode an umlaut host before anything downstream sees it: urllib sends a raw UTF-8
    # host and the server answers 400, which reads like a dead site (friseursalon-grünkorn.de).
    # Normalising here — not just in fetch_plain — keeps the rendered fetch and the emitted
    # entry file on the same ASCII host, so what we test is what changedetection gets.
    url = C.normalize_url(url)

    print(f"url    : {url}")
    rendered = args.render
    if rendered:
        try:
            html = fetch_rendered(url, args.browser_ws)
        except Exception as e:
            # "no browser" is a setup problem with a known fix, not a bug — print the fix, not
            # a traceback.
            sys.exit(f"ERROR: {e}")
    else:
        html = fetch_plain(url)
    print(f"fetched: {len(html)} bytes ({'rendered' if rendered else 'plain'})")

    ranked = collect(html, args.lang)
    real = [c for c in ranked if c['strategy'] != 'whole page']

    # A plain fetch finding nothing is the signal that the page needs a browser. So is finding
    # only *worthless* candidates: baecker-happ.de serves theme-default JSON-LD
    # ("Monday,…,Saturday 09:00-17:00") on every page, while the real per-branch hours appear
    # only after the city selector runs. Stopping at the first candidate class hid eleven
    # genuine Fulda branches behind a constant, so treat all-boilerplate as "nothing found".
    weak = bool(real) and all(L.uniform_hours(c['text'], args.lang) for c in real)
    if (not real or weak) and not rendered and not args.no_render:
        print("nothing usable in the plain HTML — retrying with the browser …")
        try:
            r_html = fetch_rendered(url, args.browser_ws)
            print(f"fetched: {len(r_html)} bytes (rendered)")
            # A render that was refused must not replace the plain result. sockpuppetbrowser
            # sends its own user agent, and hosts that answer the plain fetch with 200 answer
            # it with 403 — krieger-schrott.de does. Taking that body would report a page that
            # states its hours plainly as "no opening hours here".
            if _blocked(r_html):
                print("  the browser was served a block page, not the site — staying with "
                      "the plain fetch")
            else:
                html, rendered = r_html, True
                ranked = collect(html, args.lang)
                real = [c for c in ranked if c['strategy'] != 'whole page']
        except Exception as e:
            print(f"  browser render unavailable ({e}); staying with the plain fetch")

    if not real:
        print("\nNo opening-hours block found on this page.")
        print("Watching it would be blind — the page has nothing that can change when the\n"
              "business changes its hours. Look for another page (/kontakt,\n"
              "/oeffnungszeiten, an 'Impressum' or a branch page) before giving up.")
        if args.json:
            print(json.dumps(ranked, ensure_ascii=False, indent=2))
        return 2

    if args.json:
        print(json.dumps(ranked, ensure_ascii=False, indent=2))
        return 0

    show(ranked, args.lang)

    if args.pick:
        idx = args.pick
    elif args.best:
        idx = 1
    else:
        try:
            raw = input(f"Pick [1-{len(ranked)}] or 's' to skip: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if raw.lower().startswith('s') or not raw:
            print("skipped, nothing changed.")
            return 1
        idx = int(raw)
    if not 1 <= idx <= len(ranked):
        sys.exit(f"ERROR: pick out of range (1-{len(ranked)})")
    chosen = ranked[idx - 1]

    print(f"\nchosen: [{idx}] {chosen['strategy']}")

    if args.emit:
        from urllib.parse import urlparse
        name = args.name or (w.get("title") if args.uuid else None) or urlparse(url).netloc
        path = emit_entry(args.emit, name, url, chosen, rendered, args.lang, args.osm_id,
                          args.tags)
        print(f"wrote entry file: {path}")
        if not args.tags:
            print("note: no --tags given - the watch will have no category grouping")
        if not args.osm_id:
            print("note: no --osm-id given - the alert will carry no link into OpenStreetMap")
        print("Next: commit it, then  python3 scripts/entries_sync.py --apply")

    if not args.apply:
        recipe(url, chosen, rendered, idx, args.uuid)
        return 0
    if not args.uuid:
        sys.exit("ERROR: --apply needs --uuid (which watch to write to)")

    body = {"include_filters": [chosen['filter']] if chosen['filter'] else []}
    if args.render:
        body["fetch_backend"] = "html_webdriver"
    api.update(args.uuid, **body)
    api.recheck(args.uuid)
    print("applied + recheck queued.")
    print("Verify with: python3 scripts/watch_audit.py --uuid " + args.uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
