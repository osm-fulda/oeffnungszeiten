"""
Shared helpers for talking to changedetection: the API client (`CDIO`), URL normalisation and
the browser-like user agent.

Imported by filter_wizard, watch_audit, entries_sync, audit_report, cd_export and prescreen,
which is why it holds no state of its own and stays stdlib. It asks OSM nothing — the monitor
watches the pages it was given, and finding objects in the map is another project's job.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# browser-like UA for crawling business sites (many block non-browser UAs)
UA = {"User-Agent": "Mozilla/5.0 (compatible; changedetection-setup/1.0)"}
def today():
    return time.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Overpass
# --------------------------------------------------------------------------- #
def _idna_host(u):
    """Punycode a non-ASCII host ('physio-münsterfeld.de'). urllib sends the raw
    UTF-8 host and the server answers 400, which reads exactly like a dead site --
    the record then gets an empty watch_url and silently drops out of monitoring."""
    try:
        parts = urllib.parse.urlsplit(u)
        host = parts.hostname
        if not host or host.isascii():
            return u
        netloc = host.encode("idna").decode("ascii")
        if parts.port:
            netloc += f":{parts.port}"
        if parts.username:
            cred = parts.username + (f":{parts.password}" if parts.password else "")
            netloc = f"{cred}@{netloc}"
        return urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    except Exception:
        return u


def _encode_path(u):
    """Percent-encode a non-ASCII path or query ('/patienteninfo/öffnungszeiten/').

    urllib sends the URL as ASCII and raises UnicodeEncodeError before the request leaves,
    which a caller sorts under "unreachable" -- and it hits exactly the pages most likely to
    carry hours, because a site that spells its path in German spells it `öffnungszeiten`.
    Three of eleven such URLs in one district run failed this way.

    `%` stays safe so an already-encoded URL survives a second pass unchanged: encoding
    `%C3%B6` again would yield `%25C3%25B6`, a path no server knows.

    >>> _encode_path('https://x.de/patienteninfo/öffnungszeiten/')
    'https://x.de/patienteninfo/%C3%B6ffnungszeiten/'
    >>> _encode_path('https://x.de/patienteninfo/%C3%B6ffnungszeiten/')
    'https://x.de/patienteninfo/%C3%B6ffnungszeiten/'
    >>> _encode_path('https://x.de/suche?q=öl&s=1#öl')
    'https://x.de/suche?q=%C3%B6l&s=1#%C3%B6l'
    >>> _encode_path('https://x.de/a+b/c?d=e')
    'https://x.de/a+b/c?d=e'
    """
    try:
        t = urllib.parse.urlsplit(u)
        if u.isascii():
            return u
        return urllib.parse.urlunsplit((
            t.scheme, t.netloc,
            urllib.parse.quote(t.path, safe="/%:@!$&'()*+,;=~"),
            urllib.parse.quote(t.query, safe="/%:@!$&'()*+,;=~?="),
            urllib.parse.quote(t.fragment, safe="/%:@!$&'()*+,;=~?"),
        ))
    except Exception:
        return u


def normalize_url(u):
    """OSM website tags are often schemeless ('www.x.de') or protocol-relative
    ('//x.de'); changedetection rejects those with HTTP 400. Add https://.
    Non-ASCII hosts are punycoded for the same reason (see _idna_host), and a
    non-ASCII path or query is percent-encoded (see _encode_path).

    >>> normalize_url('www.rübsam-metall.de/öffnungszeiten')
    'https://www.xn--rbsam-metall-dlb.de/%C3%B6ffnungszeiten'
    """
    u = u.strip()
    if u.startswith("//"):
        u = "https:" + u
    elif not re.match(r"(?i)^https?://", u):
        u = "https://" + u
    return _encode_path(_idna_host(u))


class CDIO:
    def __init__(self, base_url, api_key):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.key = api_key

    def _req(self, path, method="GET", body=None):
        headers = {"x-api-key": self.key}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def list(self):
        # NB: the list endpoint does NOT return include_filters and misreports
        # fetch_backend — use get(uuid) when you need those fields accurately.
        return self._req("/watch")

    def get(self, uuid):
        """Full watch object (include_filters, fetch_backend, … are accurate here)."""
        return self._req(f"/watch/{uuid}")

    def delete(self, uuid):
        """Delete ONE watch by uuid. Only ever called targeted-by-uuid — never by
        tag string (a tag filter once matched everything and wiped all watches)."""
        return self._req(f"/watch/{uuid}", "DELETE")

    def add(self, url, tag, interval_days, fetch_backend=None):
        body = {
            "url": url,
            "tag": tag,
            # use_default MUST be False or changedetection ignores the per-watch
            # interval and falls back to the global recheck time.
            "time_between_check_use_default": False,
            "time_between_check": {"weeks": 0, "days": interval_days, "hours": 0,
                                   "minutes": 0, "seconds": 0},
        }
        if fetch_backend and fetch_backend != "system":
            body["fetch_backend"] = fetch_backend
        res = self._req("/watch", "POST", body)
        return res if isinstance(res, str) else (res or {}).get("uuid")

    def update(self, uuid, **fields):
        return self._req(f"/watch/{uuid}", "PUT", fields)

    def recheck(self, uuid):
        return self._req(f"/watch/{uuid}?recheck=1")

    def tags(self):
        """{uuid: {title: ...}}. Tags are separate objects; watches reference them by uuid,
        which is why a tag uuid is meaningless when moved to another instance."""
        return self._req("/tags") or {}

    def tag_create(self, title):
        res = self._req("/tag", "POST", {"title": title})
        return res if isinstance(res, str) else (res or {}).get("uuid")


def resolve_api_key(api_key=None, container="changedetection"):
    if api_key:
        return api_key
    env = os.environ.get("CHANGEDETECTION_API_KEY")
    if env:
        return env
    try:
        out = subprocess.check_output(
            ["docker", "exec", container, "python3", "-c",
             "import json;print(json.load(open('/datastore/changedetection.json'))"
             "['settings']['application']['api_access_token'])"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    sys.exit("ERROR: no API key. Pass --api-key, set CHANGEDETECTION_API_KEY, "
             "or ensure the container is reachable via docker exec.")
