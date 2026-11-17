# changedetection

[changedetection.io](https://changedetection.io) watching the websites of businesses in Fulda for
**opening-hours changes**, so a mapper learns that a shop changed its hours and can update
OpenStreetMap. Watches are not configured in the UI: each one is a file in `entries/` in this repository, and a
pull request adds, changes or removes one.

```
apps/changedetection/        # HelmRelease + its two values ConfigMaps + namespace
charts/changedetection/      # Chart: changedetection, headless browser, PVC, Service, sync CronJob
```

There is one way to run this, and it is the chart: `helm install` it into any cluster, a k3s on a
laptop included. One path means one image pin and one set of manifests to reason about, which is
worth more than the convenience of a second, hand-maintained one. To contribute an entry you need
no instance at all, only the wizard and, for a page that needs JavaScript, one browser container.

The datastore is one JSON file rewritten in place plus thousands of small snapshot files, so it
needs block or local storage rather than a network share. No backup: snapshot history is
disposable: a re-created watch re-establishes its baseline without alerting. What must survive is
in git, plus the API token.

**No Ingress, on purpose.** changedetection has a single shared password and no user model, and an
authenticated user can point a watch at any URL, and an exposed instance is an SSRF pivot into the
cluster.

```bash
kubectl -n changedetection port-forward svc/changedetection 5000:5000
```

**One replica.** Two processes on one datastore corrupt it; `strategy: Recreate` keeps the old pod
from overlapping the new one.

## The sync CronJob

It clones the watch repository and reconciles changedetection against the entry files. It pulls
rather than being pushed, because nothing is exposed inbound and CI therefore cannot reach the app.

Its checkout is thrown away after every run, so the slug-to-uuid mapping cannot be a file in it:
`entries_sync.py` derives the mapping instead (URL first, name against title where one URL carries
two businesses), which is what makes an hourly job safe. A stale or missing `entries/.lock.json`
is adopted, not duplicated.

**Deletion has a limit, and it announces itself.** With `sync.prune`, a watch that no entry file
claims is removed, so `git rm` in a pull request really removes it. The job refuses when more than
`sync.maxPrune` watches are unclaimed, and when no entries loaded at all: an empty checkout is a
breakage, not an instruction to empty the instance. Both the deletions and the refusals are posted
into the Matrix room, because a Job log is kept for three runs and read by nobody.

## Notifications

A relay next to changedetection owns the Matrix session, because Apprise cannot hold one against a
MAS homeserver. See [notifications.md](./notifications.md).

## Managed global settings

`deploy/global-settings.json` is the source for the noise-suppression patterns and the recheck
interval. An initContainer merges it into the datastore before the app starts, because
changedetection reads that file only at startup and overwrites it from memory afterwards.

The chart receives the same values as a second ConfigMap, which the kustomization builds from
the generated file, so the settings exist once and are copied nowhere:

```bash
python3 scripts/apply_global_settings.py --emit-values
```

writes `apps/changedetection/global-settings.values.yaml`, and that file *is* the ConfigMap. CI
fails if it no longer matches its source. Editing the settings is therefore two steps: change the
JSON, regenerate, commit both.

Expect exactly one noisy pass afterwards. A settings change moves `filter_config_hash`, which
deliberately bypasses the skip-check (`processors/text_json_diff/processor.py:436`), so every
affected watch re-baselines once. That first pass is not a regression.

**Editing the datastore by hand: stop the app first.** Watches themselves need no care: since 0.55.8
each one is its own `/datastore/<uuid>/watch.json`, written immediately and atomically, so a UI save
survives even a hard kill. `changedetection.json` is the exception, and an edit made while the app
runs is invisible to it and then overwritten from memory.

```bash
kubectl -n changedetection scale deploy/changedetection --replicas=0
#   edit the PVC, scale back to 1, then verify the value actually persisted
kubectl -n changedetection scale deploy/changedetection --replicas=1
```

## Upgrading changedetection

`.github/workflows/updatecli.yml` checks the pinned images every Tuesday. When one moved, it
rewrites the branch `deps/images` and opens or updates the issue labelled `deps` with the diff
and a compare link. Opening the pull request is a click, on purpose: Actions may not open one
here, and the click is also what makes CI run.

A browser digest can be merged as it comes. A changedetection version cannot, without a look:
it changes how a fetched page becomes text, and every watch whose text changes alerts once after
the merge. Measure that first, on the laptop, never from the VPS, where a sweep of every watched
site makes shared hosters block the address. Run both versions side by side on one copy of the
datastore, notifications stripped, and let both check everything once:

```bash
kubectl -n changedetection exec deploy/changedetection -c changedetection -- \
    tar czf - -C /datastore --exclude='*.png' --exclude='*.jpg' . > ds.tgz
mkdir ds && tar xzf ds.tgz -C ds && rm -f ds/before-update-*.tar.gz
python3 - <<'PY'                                   # no notification may leave the copy
import json; p = "ds/changedetection.json"; d = json.load(open(p))
d["settings"]["application"]["notification_urls"] = []; json.dump(d, open(p, "w"))
PY
cp -a ds ds-old && cp -a ds ds-new && podman network create cdtest
for v in old new; do
  podman run -d --rm --name browser-$v --network cdtest dgtlmoon/sockpuppetbrowser
done
podman run -d --rm --name cd-old --network cdtest -p 5010:5000 \
    -e PLAYWRIGHT_DRIVER_URL=ws://browser-old:3000 -v $PWD/ds-old:/datastore:Z \
    ghcr.io/dgtlmoon/changedetection.io:<running>
podman run -d --rm --name cd-new --network cdtest -p 5011:5000 \
    -e PLAYWRIGHT_DRIVER_URL=ws://browser-new:3000 -v $PWD/ds-new:/datastore:Z \
    ghcr.io/dgtlmoon/changedetection.io:<candidate>
curl -H "x-api-key: $KEY" 'localhost:5010/api/v1/watch?recheck_all=1'    # same for 5011
```

`$KEY` is `api_access_token` from `ds/changedetection.json`. Wait until `queue_size` in
`/api/v1/systeminfo` is 0 on both, then compare per watch whether a snapshot newer than the
recheck exists, and `last_error`:

| outcome | meaning |
|---|---|
| changed on both | the page changed; it alerts either way |
| changed on the candidate only | the upgrade changes this watch's text; it alerts once after the merge |
| error on the candidate only | the upgrade breaks this watch; fix the entry in the same pull request |

The last row is the one to act on. Going from 0.55.8 to 0.70.3, urllib3 1.26 became 2.8 and
Cloudflare answered the new client with 403 where the old one got 200, same address, same user
agent: two watches moved to `fetch_backend: html_webdriver` in the upgrade pull request. Name
the expected one-time alerts in that pull request, so nobody reads them as a broken filter.

## Secrets

The API token is mounted into the sync CronJob. Create it in-cluster, never in git:

```bash
kubectl -n changedetection create secret generic changedetection-api \
    --from-literal=api-key='<token from the UI: Settings → API>'
```
