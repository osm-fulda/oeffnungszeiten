# FILTERS.md: how to work out what a page needs to be monitored properly

changedetection triggers on **any** text change on the page it fetches. A watch is only useful if
what it captures is (a) the opening-hours block, (b) all of it, and (c) nothing else. This document
is the accumulated method for getting there: the decision ladder, the page shapes that keep
coming back, the investigation procedure, and what actually counts as proof that a filter is right.

Adding a watch to this repository: [CONTRIBUTING.md](./CONTRIBUTING.md), in German, as is
everything a contributor reads. Why it is built this
way: [CONCEPT.md](./CONCEPT.md).

---

## 0. The three failure modes

Before anything else: know what you are hunting. A watch can be wrong in three different ways, and
only the first one is visible without looking:

| Failure | Symptom | How it is found |
|---|---|---|
| **Noisy** | fires on every recheck, diff is a captcha number / rotating teaser / today-widget | a recheck-all, count diffs |
| **Blind** | never fires, page has no hours on it at all | audit **snapshot content** for time patterns |
| **Wrong** | never fires, filter captures a news box / marketing text / boilerplate constant | read what the filter captured, by hand |

"Quiet" is not "working". A run over 360 watches that reported **0 diffs** hid **77 watches** pointed
at a page with no hours at all, silent precisely because nothing on the page could ever change.
Never treat silence as evidence.

---

## 1. Decision ladder

Work top-down. Stop at the first rung that applies. Higher rungs are cheaper to build, cheaper to
run (plain fetch, no browser), and survive site redesigns better.

**The published hours win.** What a business writes on its door and on its page is the fact we
monitor; JSON-LD is a fallback, not the first choice. It is tempting as rung 1 because it is the most
durable selector, one line, immune to layout. But durability is not truth: nobody maintains an
invisible block, because nobody complains about it. Customers complain about the sign. Measured on
this instance: of 16 pages carrying JSON-LD hours, **4 contradicted
the visible page**: Müllers Backshop published `00:00-23:59` for a bakery (imported from a Google
Business Profile, `image` still points at googleusercontent.com), tegut served a chain-wide
`Mo-Sa 07:00-22:00` while the branch closes at 19:00, Pizza Capri and Eiscafé Bonifatius were
simply stale. The same neglect that makes such a block wrong today makes a watch on it **silent
tomorrow**, when the business changes its hours and updates only what people can see.

Use JSON-LD when the visible page offers nothing better: no usable anchor, only a generated class
(`iJkyRv` from styled-components), a live open/closed widget inside the block, or a candidate that
changedetection's own browser cannot find (Vergölst). Note the reason in the entry.

1. **Visible hours block**: heading-anchored (case 2), stable class/id (case 3), or day-anchored
   text (case 10); if the heading and the hours are separate boxes, the heading's next sibling
   (case 2b); if the week is split across siblings, their common ancestor (case 11)
2. **Hours only after JS** → `html_webdriver` + one of the above
3. **JSON-LD fallback** → `json:$..openingHoursSpecification`, when no visible anchor holds.
   Before choosing it, check that its hours agree with the visible ones; if they differ, the
   visible text is right and the block is unmaintained
4. **Chain / store locator** → per-branch deep link (case 5), or a keyed row when every branch
   lives on one page (case 12)
5. **Discovery landed on the wrong page** → repoint the entry's `url` at the page that has the hours
6. Then layer noise controls as needed: `sort_text_alphabetically`, `trigger_text`,
   `global_ignore_text`
7. Nothing works → **block list**: the page is recorded in `no-watch.json` with a reason and a date to look again

---

## 2. The cases

### Case 1: JSON-LD structured data (fallback, see the ladder)
**Signature:** `<script type="application/ld+json">` containing `openingHours` or
`openingHoursSpecification` (LocalBusiness / Restaurant schema).
**Filter:** `json:$..openingHoursSpecification`, else `json:$..openingHours`.
**Why it is durable:** one stable line, immune to banners, cookie bars, live open/closed widgets and layout
changes; usually works on **plain fetch** (no browser). Measured yield: of 185 unfiltered pages, 15
carried it and **9 became clean filters, 0 reverted**, far better than any other automated pass.
Where it is used, the entry records **why**, so a later reader knows the visible page offered
nothing better, rather than assuming JSON-LD was the lazy first pick.

**Three traps:**
- **WordPress theme boilerplate.** Four unrelated businesses emitted the identical
  `Monday,…,Sunday 09:00-17:00`. The resulting watch looks healthy and monitors a constant.
  Sanity-check that the hours differ per day and per business before accepting.
- **Boilerplate JSON-LD can hide real hours elsewhere on the same page.** `baecker-happ.de`
  serves that exact constant on every URL, while `/fachgeschaefte/` also carries **eleven Fulda
  branches with genuine per-branch hours**, behind a city selector. Two watches were retired as
  "publishes no hours" on the strength of the JSON-LD alone. If the JSON-LD is uniform
  `09:00-17:00`, treat the page as *unexamined*, not as answered.
- CD **strips `<script>` text**, so hours that exist only inside a framework payload (Next.js flight
  data; e-motion) are not reachable this way even though you can see them in the HTML.

**When probing by hand, print more than the top candidate.** The same Happ page ranked the
boilerplate JSON-LD first and the real branch-hours widget second; a probe script that printed
only `candidates[0]` reported "no hours" for a page covered in them.

### Case 2: heading-anchored block in plain HTML
**Signature:** an "Öffnungszeiten" heading with the times in or next to it, present in the raw
HTML (no JS needed).
**Filter:** `xpath:(//h3[contains(normalize-space(.),"Öffnungszeiten")])/parent::*`
**Automated by** the wizard's Strategy 2 (`filter_wizard.py:149`). The heuristic it encodes,
worth knowing because you will apply it by hand too:

> for each hours keyword × each of `h1…h6, strong, b, p, span, div, li, td`
> → consider the heading's **parent**, then the heading itself
> → keep only if it contains a time, is 10–1400 chars, and is **< 70 % of the page's total text**
> → the combined match across all XPath hits must be < 1700 chars
> → among survivors, **prefer the shortest**

That is: *the smallest element under the hours heading that still contains a time.* Typical yield
11–14 filters per batch of candidates.

### Case 2b: heading and hours are separate sibling blocks
**Signature:** Case 2 finds the heading and captures the word alone. The heading, its parent and
its grandparent all read `Öffnungszeiten` and hold no time, so the page looks as if it published
no hours — while a browser plainly shows them one box further down. Page builders cause this:
Duda (Krieger Schrott), Squarespace and Elementor put every section in its own row, and the
heading row is a sibling of the hours row, not its ancestor.
**Filter:** anchor on the heading's own box and step sideways.
```
xpath://*[@id="Offnungszeiten"]/following-sibling::*[1]
```
Deluxe Barbier is the same shape one level higher:
`//h2[…"öffnungszeiten"]/ancestor::section[1]/following-sibling::section[1]`.
**Automated by** the wizard's Strategy 2b: from a heading that carries no time, climb through
ancestors that hold the heading **and nothing else**, and take the first one whose next sibling
carries hours.
**Both guards are load-bearing.** Without the "nothing else" rule the climb starts at a nav link
— `deluxebarbier.de` lists `Öffnungszeiten` in its menu — reaches a page-level container and
captures whatever section follows it. Without a width cap on the sibling, that capture is the
whole `<main>`, which does contain hours and outranked the correct filter. Measured over 80
existing entries, 78 of them reachable: with both guards, rank 1 is unchanged on every one.

### Case 3: stable class or id container
**Signature:** the page ships a purpose-built hours element with a human-authored (not generated)
class or id.
**Filter:** plain CSS. In use: `.detail-dealer-open-hours` (Pappert), `#home__times`,
`.seitencontent` (gruemel), `.penci-working-hours` (A7 Bikestore), `.location-detail-openday` (Davis),
`.et_pb_text_1` (Os Sabores), `.close__meta` (RED Sports).
Prefer this over Case 2 when it exists: it is shorter and reads better in the UI.

### Case 4: hours render only under JS
**Signature:** plain fetch shows a shell / spinner / no times; the browser shows hours.
**Fix:** selector + `fetch_backend: html_webdriver`.
**Check the render actually rendered the site.** sockpuppetbrowser sends its own user agent, and
hosts that answer a plain fetch with 200 answer it with 403 — `krieger-schrott.de` does, 125
bytes of `403 Forbidden`. A 403 body carries no hours, so a blocked render is indistinguishable
from a page without hours unless you look. The wizard now keeps the plain result when the render
comes back blocked, and says so.
**Caveats:**
- CD's browser renders some SPAs in **English** (Davis), so German-keyword XPaths silently match
  nothing. Anchor on class, not text.
- Rendering in bulk clears no backlog. The heading-anchored finder with JS rendered first fails
  on exactly the markup it fails on without JS: **3 hits out of 188 candidates (1.6 %)**, one of
  which auto-reverted. Render the page you are working on, not a queue.

### Case 5: chain / store locator
**Signature:** many businesses share one corporate URL; the page is a locator, not a branch page.
**Fix:** find the per-branch deep link, split into separate `manual` watches.
In use: Subway `restaurants.subway.com/de/deutschland/he/fulda/<street>` (+ `table.c-hours-details`
and `sort_text_alphabetically`, rotation-proof), Pappert `/dealer/<slug>/`, meliva
`/standort/<slug>/`, Maritim per-outlet sections, tredy via `data-store-id` on `/storefinder`.
**Matching a record to its branch page** is the real work, two approaches that worked:
- **coordinates via Overpass** (Pappert: all 7 matched ≤ 30 m)
- **the practitioners' names** (meliva: Orthopädie Fulda → `dalberg-klinik-fulda`, identified via
  Schiffhauer/Kegel/Weghenkel)

### Case 6: discovery landed on the wrong page
**Signature:** watch URL is plausible but the hours belong to someone else, or there are none.
Automatic subpage discovery scored the first `Kontakt`-ish href, which on many sites is a
**site-wide footer link** present on every page. That is why the entry names its page itself.
**Fix:** repoint the entry's `url` at the page that carries the hours. The entry is the source,
so nothing re-discovers over it.
**Seen:** gruemel ×2 → accessibility statement (and, sharing a URL, they shared one **watch**);
`fulda.de/kontakt` → **Bürgerbüro** hours for both Vonderau Museum records; `re-gruppe.de/service/
kontakt` → the **operator's office** hours (Mo–Fr 9–16) for two swimming pools; `hotel-esperanto.de/
kontakt/` → three restaurants onto a page with no hours; Karlchen vom Dach → an Elementor **popup
trigger** (`#elementor-action%3A…`).
**Grep for the bug shape:** two records with the same `cd_uuid`.

### Case 7: content reorders every day
**Signature:** diff shows the same lines in a different order, or today's line duplicated.
**Fix:** `sort_text_alphabetically`. In use: Subway ×2, Matratzen Concord, meliva ×2.
**Limit:** it hides re-ordering, **not duplication**. Davis had a weekly panel plus a sibling
`.location-detail-opentime.highlight` "today" widget inside the same wrapper, today's line appeared
twice and moved daily. Sorting masked the order but not the duplicate; the wrapper had to change.

**The wizard flags the shape at creation time**, because that is where it is cheapest and least
visible: an unlabelled time before the first weekday costs 20 points and reads *"a 'today' widget
in the same wrapper, which rewrites itself daily"*. Adler had two candidates tied at 141, the
wrapper listed first, and the wrapper was taken; it now scores 121 against the table's 141. Only a
*bare* value counts — "Täglich von 17:00 bis 00:00 Uhr" ahead of the table is a statement about
the week and stays unflagged. Measured against all 559 samples: 4 hits, every one a watch that
already sorts, where the times group ahead of the days by definition.

**Proof, before changing anything:** fetching the page proves nothing, because the rotation
depends on the time of day it is fetched. The stored snapshots do prove it. Per snapshot, hold the
checksum of the raw text against the checksum of its lines sorted:

| | |
|---|---|
| raw differs, sorted identical | rotation — `sort_text_alphabetically` |
| raw and sorted both differ | a real change — read it |

`scripts/rotation_check.py` is that comparison over every watch, and `--uuid <uuid>` runs it on
the one that just fired and prints the lines that actually differ. It fetches no page. After
switching sorting on, expect **exactly one** more alarm per watch: the first sorted snapshot runs
against the last unsorted one. The tool names that state `SETTLED` so it is not mistaken for a
filter that failed.

### Case 7b: the same text, decoded differently

**Signature:** a diff that changes only invisible or replacement characters, `4���pm` becoming
`4 pm`, or an umlaut turning into mojibake and back. The page did not change; the decoding did.

**Cause seen here:** 9gg.de writes a narrow no-break space (U+202F, `e2 80 af`) between the hour and
`pm`, and sends **no charset in the HTTP header**, only in a `<meta>` tag. Whether that byte survives
depends on what reads it, and a changedetection restart was enough to flip the result. Measured on
`zum-biereck.9gg.de`: the old snapshot held three U+FFFD, the next one a plain space, same page.

**What not to do:** treat it as an hours change, or widen the filter. **What helps:** compare against
the previous snapshot before reading anything into a diff: this shape is recognisable in a second.
A `/�/` line in `global_ignore_text` would silence it globally, but every global-settings edit
re-baselines all watches, which is out of proportion to four affected pages.

### Case 7c: the page changes language

**Signature:** every line of the hours block changes at once and the times are identical,
`Monday 8:30 am–7 pm` against `Montag 8:30 am–7 pm`. Nothing about the business changed; the
generator rendered the day names in another language.

**Cause seen here:** 9gg.de serves `<html lang="de">` with English day names, or German ones,
depending on what its Cloudflare cache holds. `vary` names `Accept-Encoding,User-Agent` and not
the language, so the language is not negotiated at all: sending `Accept-Language: de-DE` changes
nothing, and neither does the user agent. All four Fulda pages on that platform flip together.

**Fix: pin the language in the watch URL.** `?hl=de` forces German on 9gg.de, measured stable
over repeated fetches, while `?lang=` and `?locale=` are ignored and `/de/` is 404. The entry
carries the URL with the parameter. `url` is a baseline key, so the sync announces the change and
the next check re-baselines once.

**Why not filter it away instead:** the day name and its time share a line, so any rule that
drops the language drops the hours with it. Narrowing the capture to the times alone would keep
the watch quiet, but a diff of seven bare time ranges is not something a mapper can act on.

**When there is no such parameter,** the honest answer is that the watch reports the flip and a
human reads it in a second. Do not widen the filter to hide it.

### Case 8: server swaps content between concurrent requests
**Signature:** two watches on the same host false-diff forever, each showing the other's page.
gruemel.de (IIS) returns the *same* page to two simultaneous requests for different URLs. Reproduced
with **separate cookie jars**, so it is server-side global state, not a session problem, and no
fetch setting avoids it.
**Fix:** give each watch a **`trigger_text`** page-identity marker, a string only that page's
filtered text contains. `trigger_text` blocks the change **and** skips the `previous_md5` update
when the marker is absent (`processors/text_json_diff/processor.py`), so a swapped fetch is
discarded instead of poisoning the baseline.
**Cost:** if the marker ever disappears the watch goes quiet. Pick a durable string.

### Case 10: day-anchored text (no heading, no useful class)
**Signature:** the page prints hours in a bare `<p>` or `<div>` with no "Öffnungszeiten"
heading anywhere and no meaningful class: Reinholz Kaffeerösterei's footer reads
`Kaffeeladen im Steinweg: Mo-Sa: 10 - 18 Uhr`. Cases 2 and 3 are both structurally blind to
this, and it is the single largest group in the remaining backlog.
**How the wizard solves it:** find the *innermost* element whose own text looks like hours,
then anchor on the nearest durable ancestor class, skipping grid/utility classes (`col-12`,
`row`, `d-flex`) and page-builder GUIDs. If no such class exists, anchor on the weekday word
itself, restricted to the innermost match:
```
//p[contains(translate(.,"MONTAG…","montag…"),"montag")][not(.//p[contains(…,"montag")])]
```
The `not(...)` clause is essential: without it every ancestor matches too and the filter
swallows the page.
**Every anchor is validated by what it captures.** An ancestor class like `fl-module` matches
dozens of unrelated blocks; taking it unchecked turned a 71-character filter into the whole
page.
**When every anchor fails, the generated one is still offered**, flagged `avoid — brittle
selector`. A rejected candidate you can read beats a page reported as publishing no hours:
that answer is not a warning, it is wrong, and it is what closed the first attempt at Krieger
Schrott, whose Duda markup names every box `u_1535202874`. Take it only when nothing else
exists, and write the reason in the entry's `note`.

### Case 11: common ancestor (the week is split across siblings)
**Signature:** no single element holds the whole week. Robe's Bike House has
`Dienstag – Freitag …` in one `<p>` and `Samstag …` in the next, so every individual candidate
is an incomplete filter and would silently miss half the changes.
**Fix:** anchor the **lowest common ancestor** of the hour-bearing elements.
**Why it matters here:** the only element that did cover both was a Beaver Builder div whose id
was `fl-icon-text-cjg0i7ku1qhr`, complete but brittle. The durable answer is the LCA reached by
a day-word anchor. Watch for this whenever a candidate reports fewer weekdays than the page
visibly shows.

### Case 12: keyed row (one branch inside a page listing many)
**Signature:** a chain publishes **all** branches on one page, often behind a city selector, so
Case 5's per-branch URL does not exist. `baecker-happ.de/fachgeschaefte/` lists eleven Fulda
shops, each with its own hours.
**Fix:** filter to one row by a key unique to that branch: the street address works best,
combined with a time so the address block alone is not matched:
```
xpath://div[contains(.,"Kanalstraße 54") and contains(.,"Uhr")]
          [not(.//div[contains(.,"Kanalstraße 54") and contains(.,"Uhr")])]
```
Get the address from OSM (`addr:street` + `addr:housenumber`) so the key matches the right
business. Prefer this to filtering the whole branch-list widget: one watch per branch means an
alert names the shop that actually changed.

### Case 9: site-wide noise classes
Handled globally, not per watch, via `settings.application.global_ignore_text` (25 patterns):
- German **math captchas** ("bitte addieren", "was ist die summe", "summe aus N und M",
  "wieviel ist N +/- M", "SPAM-Schutz die Zahl N", "3 + 5 = ?"), Divi/Contao contact forms
- **live open/closed widgets** ("we're open", "closes at N", "öffnet um/bald", "schließt um",
  "derzeit geschlossen", "jetzt geöffnet/geschlossen", "wir öffnen heute um 16:00")
- the page **echoing our own IP** back (PROSOL `Client-IP:…`)

Patterns are kept narrow on purpose so a legitimate weekly `Montag: geschlossen` still triggers.

Editing global settings has two hard rules, and both are in
[docs/changedetection.md](./docs/changedetection.md).

### Case 13: the snapshot is raw HTML, not text
Symptom: the stored snapshot shows markup (`<div class="row g-0">`), the capture is ten times
longer than the text it contains, and no `ignore_text` pattern ever matches. The filter is fine:
changedetection decided the page was **plaintext** and skipped its HTML-to-text step.

The decision lives in `processors/magic.py`: a page counts as HTML if one of
`<!doctype html`, `<html`, `<head`, `<body`, `<script`, `<iframe`, `<div` appears in the **first
200 bytes**, or if the Content-Type header is exactly `text/html`. A header of
`text/html; charset=UTF-8` is *not* an exact match, so everything rests on those 200 bytes,
and Shopware opens its pages with Twig include comments:

```
<!-- INCLUDE BEGIN @Storefront/storefront/page/content/index.html.twig (vendor/…) -->
```

No pattern matches, the header check fails, and the fallback `http_content_header.startswith('text/')`
marks it plaintext. Fix: set `fetch_backend: html_webdriver`. The browser returns a normal
serialised DOM starting with `<html>`, the same filter then yields clean text
(elektrozigge.de: 3333 chars of markup → 248 chars of hours).

Check for it with a per-watch snapshot read, not the UI diff:

```bash
curl -s -H "x-api-key: $KEY" "$CD/api/v1/watch/<uuid>/history/<ts>" | head -c 200
```

### Not a case: absence
No hours published anywhere, anti-bot 403 in all modes (lieferando/DataDome class), dead domain:
write the object into [`no-watch.json`](./no-watch.json) instead, with the reason, the date and a
`recheck` (`scripts/no_watch.py`). An object belongs to `entries/` or to that list, never to both,
and CI fails if it appears twice. The reason decides whether it is ever looked at again: a property
of the **business** (publishes nothing, appointment only) is answered by looking again later, a property
of the **site** (403, dead domain) by a later fetch. Both are cheaper than re-examining the same
shop from scratch every pass, which is what an unrecorded absence costs.

---

## 3. Investigation procedure: one page

### The fast path: two tools that do this for you

Most of §3 is only needed when the tools come up short. Try them first: neither needs browser
devtools, and neither needs anything beyond the page and a changedetection instance.

**`filter_wizard.py`: choose a filter by reading text, not selectors.** It runs every
strategy in §2 and prints the *text each candidate would capture* as a numbered menu, with
warnings attached. An admin who can read "Mo-Sa: 10 - 18 Uhr" can pick correctly without
knowing what an XPath is.

```bash
python3 scripts/filter_wizard.py https://example.de/kontakt      # explore a URL
python3 scripts/filter_wizard.py --uuid <uuid>                   # existing watch
python3 scripts/filter_wizard.py --uuid <uuid> --pick 2 --apply  # write the choice
python3 scripts/filter_wizard.py <url> --render                  # JS-rendered page
python3 scripts/filter_wizard.py <url> --lang en                 # English page
```

It flags what it cannot judge for you: `brittle selector`, `wide (>70% of the page)`,
`only N weekday(s)`, `same hours every day`,
`captures the same hours N×`. Nothing is written without `--apply`. "Whole page" is always
offered last, so *no filter* stays a visible, deliberate choice.

**`watch_audit.py`: find the watches that only look healthy.** Reads each watch's stored
snapshot and judges it against the four criteria in §4, in plain language.

```bash
python3 scripts/watch_audit.py                                # every watch, worst first
python3 scripts/watch_audit.py --only red                     # just the broken ones
python3 scripts/watch_audit.py --html audit.html              # report for a browser
```

RED = broken, blocked, or watching a page with no hours (can never fire).
AMBER = works but incomplete, duplicated, boilerplate, unfiltered or currently noisy.
Read-only; it never writes. A full pass takes about two seconds.

Both share `scripts/hours_lang.py`, which holds the German/English keyword lists and the
time/weekday regexes. Add a language there and both tools gain it.

**Or click it in your own instance.** changedetection has a visual selector, and this repository's
instance is not reachable, so run one yourself, pick the block by clicking, and export what you
built:

```bash
docker run --rm -p 5000:5000 -v cd-data:/datastore ghcr.io/dgtlmoon/changedetection.io:0.55.8
python3 scripts/cd_export.py --split entries --base-url http://localhost:5000
```

That writes one entry file per watch, `captured_sample` included, ready for a pull request. The
visual selector likes generated class names, so read what it produced before committing it.

### Step 0: look at what the watch already captures

```bash
curl -s -H "x-api-key: $KEY" $CD/api/v1/watch/<uuid>/history        # -> {timestamp: file}
curl -s -H "x-api-key: $KEY" $CD/api/v1/watch/<uuid>/history/<ts>   # -> the captured text
```

Read the snapshot **before** touching anything; most "mystery" watches are explained by it. Always
fetch the watch itself per uuid, never from the list endpoint: the list omits `include_filters`,
`notification_urls`, `notification_body` and `extract_text`, and misreports `fetch_backend`.

### Step 1: does the page have hours at all?

Use `hours_lang.py`; do not hand-roll this regex, it is wrong in more ways than it looks.
`\d{1,2}[:.]\d{2}` alone under-counts badly, which once claimed 92 hours-free watches where the
true count was 77. Every one of these formats appears on real Fulda pages and each broke a
naive pattern:

| Format | Seen on | What a naive regex does |
|---|---|---|
| `10 - 18 Uhr` | most German sites | misses it, no minutes at all |
| `Mo-Di 11-24h` | Heimat | misses it, `h` suffix, no minutes |
| `11 : 00 - 22 : 00` | Aiko Sushibar | misses it, spaces around the colon |
| `donnerstags8:00` | Jordan's Mensa | misses the **weekday**, `\b` fails between `s` and `8` |
| `212.110.223.68` | anti-bot block pages | **false hit** inside the IP address |
| `27.03.2025` | closure notices | false hit unless dates are excluded |
| `"opens": "09:00:00"` | JSON-LD `openingHoursSpecification` (KIND) | misses it, the lookahead guarding against IPs trips over the second colon, so a working watch is reported as having no hours at all |

```python
import hours_lang as L
L.time_matches(text)      # [(token, surrounding context), …], context so you can CHECK it
L.weekdays(text, "de")    # ['Mon','Thu','Fri'], distinct days, abbreviations only if a time exists
L.hours_score(text)       # 0 = not an hours block
L.looks_blocked(text)     # anti-bot interstitial, not content
```

**Always print the matched context before trusting a hit**: that is why `time_matches`
returns it. A bare `captcha` or `forbidden` in the text is *not* proof of a block page either:
those words appear in ordinary contact forms, which marked a working shop page as blocked
until the phrase list was tightened and a length guard added.

Two more measurement traps, both of which produced confident wrong answers:

- **Weekday detection must be bilingual even on German pages.** A `json:` filter renders as
  `https://schema.org/Monday`, so a German-only day list reported *every* JSON-LD watch as
  "times found but no weekday named". Acting on that would have replaced working JSON-LD
  filters with a nav menu and a contact card. `hours_lang` now always counts English full
  names as well.
- **Uniform hours are not evidence of boilerplate.** Aldi really does open 08:00–21:00 seven
  days a week. Flagging uniformity alone produced 22 false alarms against 2 real findings; it
  only means something when the range is the schema.org default `09:00-17:00`, or when the
  identical text also appears on an unrelated watch.

Rapid probing also trips anti-bot on some hosts, so if block-page shapes appear, back off and
re-probe gently.

No hours in a plain fetch → render the page before concluding anything (Step 2).

### Step 2: by hand, when the wizard came up short

**JSON-LD first.** Search the HTML for `application/ld+json` blocks containing `openingHours`. Found
one → the filter is `json:$..openingHoursSpecification` (or `$..openingHours`), and then check that
the values differ per day and are not the `Monday,…,Sunday 09:00-17:00` boilerplate.

**Empty in plain HTML → render it.** One throwaway browser is enough, and the wizard finds it on the
default port:

```bash
docker run --rm -p 3000:3000 dgtlmoon/sockpuppetbrowser
python3 scripts/filter_wizard.py <url> --render
```

Rendering runs from here through `cdp_render.py`, not inside changedetection, so the cluster's
browser stays free for the watches it serves. Note the locale: CD's own browser does not always
honour `de-DE` (Davis renders English), so read the *rendered* DOM rather than assuming.

**Anchor on text or an authored class**, never on a page-builder name
(`.text-46bf3150-186a-…`, `elementor-element-224ed87`, which changes at the next site edit) and
never on an absolute XPath (`/html/body/div[2]/div[6]` blinded Eye Eye Optik, whose hours were in
plain HTML all along). Two shapes that keep working:

- text-anchored: `xpath://p[contains(.,"Montag") and contains(.,"Freitag") and contains(.,"Uhr")]`
- space-padded class test, when a page ships a desktop and a mobile copy of the same block:
  `xpath://div[contains(concat(" ",normalize-space(@class)," ")," randspalte ")]//div[…]`

**Apply, recheck, verify (§4), then write the filter into the entry file.** In Python that is
`C.CDIO(base, key).update(uuid, include_filters=[...])`, `.recheck(uuid)`. A fix that stays in the
UI is reverted by the next hourly sync.

---

## 4. What counts as proof

Every automatic check reduces to *"does the filtered snapshot still contain a time?"*. That
catches **blinding** and nothing else. It has passed, in production:
- a practice **news box** (job ad + "Wir machen Urlaub" + "keine Neupatienten", all contain times)
- a **marketing paragraph**
- WordPress **boilerplate JSON-LD** monitoring a constant
- Davis's wrapper that **duplicated** today's line and moved it daily

So after applying, read the captured text and confirm all four:

1. **It is the hours block**: not news, not a job ad, not a service list.
2. **It is complete**: a filter grabbing Mo–Fr while Sa/So sit in a sibling element looks perfectly
   healthy and silently misses half the changes. Count distinct weekdays.
3. **It is not a constant**: values differ per day and are not shared with unrelated businesses.
4. **It is stable across two rechecks**: no rotation, no today-widget, no captcha number.

### Do NOT verify an ignore rule by diffing stored snapshots

`ignore_text` is applied only to `text_for_checksuming`
(`processors/text_json_diff/processor.py:568`); the **stored snapshot keeps the ignored lines**
unless the global `strip_ignored_lines` is enabled (it is not). A diff of two snapshots therefore
still *shows* an ignored captcha number changing while changedetection has already discounted it.
This cost a debugging cycle. The valid test is whether **`last_changed` advances** on a later check.

### A "recheck all" is not proof every watch ran

One recheck-all **silently skipped 7 watches**, they still carried timestamps from days earlier.
Always reconcile `last_checked` against the batch before drawing conclusions from a diff count.

---
