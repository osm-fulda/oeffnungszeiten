# Repairing a watch

The weekly report (`Wochenreport: N Watches brauchen Aufmerksamkeit`) answers its own message with
one reply per broken watch, each with a first move and a link to its section below.
This page is the long version: how to find the cause behind a finding, fix it in the right place,
and prove the fix both before and after it is merged. It assumes `source scripts/cd_env.sh`, so
`CD_BASE_URL` and `CHANGEDETECTION_API_KEY` are set; [FILTERS.md](../FILTERS.md) covers how to
build a filter, this page covers everything around it.

## The loop

Every finding goes through the same five steps. The finding only decides what happens in step 2.

**1. Group before you start.** Read the whole report and sort the findings by host. Several
findings on one host, or on one platform, are one cause: four `9gg.de` restaurants failing in the
same week is one template change, two `emaillierwerk-fulda.de` pages with `Max retries exceeded`
are one dead server, two `gersfeld.de` pages with 404 are one site relaunch. Diagnose one of
them, fix that one, then apply the same change to the rest. A finding the report puts under the
wrong heading is still sorted correctly by its host.

**2. Reproduce what changedetection sees.** Not what your browser shows: the browser runs
JavaScript, sends a different user agent, and comes from a home address. The section for the
finding below says which of these to use.

```bash
python3 scripts/watch_audit.py --uuid <uuid>                  # the finding, on its own
python3 scripts/filter_wizard.py --uuid <uuid> --json         # candidates on the live page
python3 scripts/validate_entries.py --live --only entries/<name>.json \
    --browser-ws ws://localhost:3000                          # the entry, end to end
```

For the last one, run a browser of your own (`docker run --rm -p 3000:3000
dgtlmoon/sockpuppetbrowser`), never the cluster's (it serves every `html_webdriver` watch, see
[Operating it](./notifications.md#operating-it)), and install `elementpath` (`pip install
elementpath`). Without it the check runs on lxml only, and lxml is not what changedetection runs
for `xpath:`.

**3. Fix the entry, not the watch.** `entries/` is the source. The sync reconciles every hour and
puts the file's filter back, so a fix made in the UI, or with `filter_wizard.py --apply`, lasts
until the next `:17`. Write the result into the file:

```bash
python3 scripts/filter_wizard.py --uuid <uuid> --pick N --emit /tmp/x   # then copy the fields
```

and say in the entry's `note` what changed and why, with the date. The note is what the next
person reads when this watch breaks again.

**4. Prove it before the pull request.** `validate_entries.py --live` with a browser and
`elementpath`, on the changed files. What it catches that a look at the page does not:

| check | what slips through without it |
|---|---|
| matched in the **rendered** page | an `html_webdriver` entry whose filter only matches in markup the browser never produces |
| lxml **and** elementpath match | `preceding-sibling::*[…][1]` selects the right paragraphs in lxml and nothing under elementpath; the cluster reports `no filters were found` on a page nobody touched |
| no XML declaration on a plain fetch | changedetection fails on every check with `Unicode strings with encoding declaration are not supported`, before any filter runs |
| a time in the capture | a filter that matches a heading, a news box or an empty wrapper |

When the cause is inside changedetection itself, reproduce it there, with its own functions, on
the page as it fetched it:

```bash
kubectl -n changedetection exec -i deploy/changedetection -c changedetection -- python3 - <<'EOF'
import requests
from changedetectionio import html_tools as H
html = requests.get(URL, headers={"User-Agent": UA}, timeout=30).text
print(H.html_to_text(H.xpath_filter(XPATH, html)))      # xpath:   (elementpath)
print(H.html_to_text(H.xpath1_filter(XPATH, html)))     # xpath1:  (lxml)
EOF
```

`UA` is `requests.default_ua.html_requests` from
[deploy/global-settings.json](../deploy/global-settings.json).

**5. Confirm after the merge.** The sync rechecks every watch whose URL, filter or backend changed
right after updating it, so the answer is there minutes after the `:17` run, not a week later in
the next report:

```bash
python3 scripts/watch_audit.py --uuid <uuid>              # should no longer be RED
python3 scripts/entries_sync.py                           # plan: create 0 · update 0 · delete 0
```

Expect one alert per repaired watch: the first capture through the new filter differs from the
last one through the old, and the sync announces it in the thread beforehand
([The baseline swap](./notifications.md#the-baseline-swap-after-a-filter-change)).

**Removing a watch** is also a fix. Delete the entry file and record the object in
[no-watch.json](../no-watch.json) with its reason and a `recheck` date (see
[FILTERS.md, Not a case: absence](../FILTERS.md#not-a-case-absence)). The sync refuses to delete
more than five watches in one run; raise `--max-prune` for that run, do not remove the guard.

## Findings

### 403: the host refuses

Compare the same request from two places, with the user agent changedetection sends:

```bash
UA=$(python3 -c "import json;print(json.load(open('deploy/global-settings.json'))['requests']['default_ua']['html_requests'])")
curl -s -o /dev/null -w '%{http_code}\n' -A "$UA" <url>                     # from here
ssh vps "curl -s -o /dev/null -w '%{http_code}\n' -A '$UA' '<url>'"           # from the VPS
```

- **200 from both**: the user agent in the cluster is not the one you just sent. Check the watch's
  `fetch_backend`; the browser backend sends whatever the sockpuppet image carries.
- **403 from both**: the user agent is the problem. If 403s cluster across unrelated hosts, the
  pinned Chrome version has aged; bump it in `deploy/global-settings.json`.
- **200 here, 403 from the VPS**: the host blocks datacenter addresses. Nothing in the entry fixes
  that. Remove the watch, `reason: datacenter-block`, `recheck: on-relocation`.
- **403 here as well, every backend**: anti-bot. Remove it, `reason: anti-bot`.

### 404: the page is gone

Look for the successor before removing anything: the site's own navigation, `/kontakt`,
`/oeffnungszeiten`, the branch finder. A relaunch usually keeps the content under a new path, and
several 404s on one host in the same week are almost always that. Found → new `url`, then the
filter for the new page. Not found → `reason: site-unreachable` with a recheck date.

### 5xx: the server fails

One 503 is the site's bad day; wait for the next run before touching anything. The same 5xx in two
consecutive reports is a dead site: treat it as a 404.

### No answer: Max retries, DNS, TLS

`HTTPSConnectionPool(...): Max retries exceeded` means no HTTP answer at all: the server is down,
the name no longer resolves, or the certificate is broken. The text after `Caused by` says which.
Run the curl from above from both places. Down from both → wait one run, then
`reason: site-unreachable`. Down only from the VPS → the same split as a datacenter 403.

### Exception: the parser fails

The fetch worked; something after it did not. The text after `Exception:` decides:

- **nothing at all**: elementpath asserted on content behind `</html>`. Same expression with
  `xpath1:` ([FILTERS.md, Case 14](../FILTERS.md#case-14-exception-with-nothing-after-it)).
- **`Unicode strings with encoding declaration are not supported`**: the page is XHTML and starts
  with `<?xml …?>`; changedetection hands the decoded text to lxml, which refuses it. Set
  `fetch_backend: html_webdriver`: the browser returns the DOM without the declaration.
- **`No parsable JSON found in this document`**: a `json:` filter on a page that no longer carries
  the JSON-LD block. Run the wizard; the hours are usually still visible as HTML.
- **anything else**: reproduce in the pod (step 4), the traceback names the cause.

### no filters were found

changedetection fetched the page and the filter matched nothing. There are three reasons, and the
wrong guess costs a pull request:

| what you see | cause | fix |
|---|---|---|
| `validate_entries.py --live` fails without a browser and passes with one | the hours now arrive by JavaScript | `fetch_backend: html_webdriver` |
| lxml matches, elementpath does not | the expression relies on something XPath 3 reads differently | rewrite without it, or `xpath1:` |
| nothing matches anywhere | the site was rebuilt, the anchor is gone | `filter_wizard.py --uuid`, new filter |

The third is the common one. Before picking a new candidate, read the old `note`: an anchor was
often chosen to avoid a trap (a weather widget with the same class, a language switch), and the
new filter has to avoid it too. After six consecutive misses changedetection sends its own
`CSS/xPath filter was not present` message; the weekly report usually gets there first.

### No hours in the capture

`no opening hours on this page at all` / `captures no time`: the filter matches, but what it
captures cannot ever show a change of hours. Either the business took the hours off this page
(look for a better one, FILTERS.md §3 Step 1), or the filter slipped onto a neighbour element
after a site edit (run the wizard). If the business publishes no hours anywhere, the watch is
blind for good: remove it, `reason: no-hours-on-page`.

### Incomplete or duplicated capture

`no weekday named`, `only N weekday(s)`, `the same hours are captured N×`, `captures text
identical to N other watch(es)`, `every day shows the same`: the filter works but sits on the
wrong element. These are filter-building questions; FILTERS.md has a case for each (11 common
ancestor, 12 keyed row). They are AMBER unless they stop the watch from ever firing, so most of
them reach the report only when they do.

### connect_over_cdp

Not a problem of the page. The report rechecks every fetch error before sending, so one that still
stands survived a second fetch: look at the browser pod (`kubectl -n changedetection get pods`),
not at the entry.

### 429: rate limited

Rate limiting, not a block. If it survives the recheck and stays over several runs, it is usually
the hoster limiting the cluster's address across all its customers, not this one page.
