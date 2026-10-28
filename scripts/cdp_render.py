#!/usr/bin/env python3
"""
cdp_render.py — render a page through sockpuppetbrowser, talking CDP directly.

Why this exists: rendering used to run *inside* the changedetection container, because the
browser speaks CDP over a WebSocket and there was no stdlib client to drive it. That was only
half true. sockpuppetbrowser rejects plain HTTP (426 on every path) and does not implement the
Playwright wire protocol — but the render snippet only ever used `connect_over_cdp()`, i.e.
**raw CDP**, and raw CDP over a WebSocket is something ~120 lines of stdlib can do.

The consequence is the point: rendering no longer needs changedetection, `docker cp` or
`docker exec`. One container is enough —

    docker run --rm -p 3000:3000 dgtlmoon/sockpuppetbrowser
    python3 scripts/filter_wizard.py <url> --browser-ws ws://localhost:3000

— which also means CI can render, so entries with `fetch_backend: html_webdriver` become
live-verifiable instead of being waved through with "may be anti-bot or JS-only".

Against the cluster, port-forward the browser Service and use the same flag:

    kubectl -n changedetection port-forward svc/changedetection-browser 3000:3000

Stdlib only, no dependencies.
"""
import base64
import json
import os
import socket
import struct
import sys
import time
from urllib.parse import urlsplit

DEFAULT_WS = "ws://localhost:3000"
# changedetection's own render waits after domcontentloaded because hours often arrive with a
# late XHR (store locators). Keep the same order of magnitude or we capture a spinner.
SETTLE_SECONDS = 3.5
# A bound against a runaway page, not a size limit: changedetection keeps the whole DOM, and a
# render cut short makes a working filter look wrong. tabla-restaurant.de inlines its images and
# renders 6.5 MB, with the hours in the footer at the very end.
MAX_HTML = 30_000_000


class WSError(RuntimeError):
    pass


class WS:
    """Minimal RFC 6455 client: handshake, masked text frames, reassembly, ping/pong.

    Only what CDP needs — text frames, no extensions, no compression. Server frames are never
    masked, client frames always are.
    """

    def __init__(self, url, timeout=45):
        u = urlsplit(url)
        if u.scheme not in ("ws", "http", ""):
            raise WSError(f"only ws:// is supported, got {url!r}")
        host, port = u.hostname, u.port or 3000
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise WSError(f"cannot reach {host}:{port} ({e})") from None
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path or '/'} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("connection closed during handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WSError(f"handshake refused: {status}")
        self.status = status

    def _recv_exactly(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise WSError("connection closed mid-frame")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode()
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x81, n | 0x80)
        elif n < 1 << 16:
            header = struct.pack("!BBH", 0x81, 126 | 0x80, n)
        else:
            header = struct.pack("!BBQ", 0x81, 127 | 0x80, n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self):
        """One complete text message. Answers pings, raises on close."""
        parts = []
        while True:
            b0, b1 = self._recv_exactly(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exactly(8))[0]
            data = self._recv_exactly(length) if length else b""
            if opcode == 0x9:                                   # ping -> pong
                self.sock.sendall(struct.pack("!BB", 0x8A, len(data) | 0x80)
                                  + b"\x00\x00\x00\x00" + data)
                continue
            if opcode == 0xA:                                   # stray pong
                continue
            if opcode == 0x8:
                raise WSError("browser closed the connection")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.sendall(b"\x88\x80\x00\x00\x00\x00")
        except OSError:
            pass
        finally:
            self.sock.close()


class CDP:
    """Request/response over one WebSocket, with event waiting.

    CDP interleaves command replies and events on the same socket, so a reader looking for one
    has to set the other aside. Events go to a separate list, never back into the read path:
    parking them in the same queue the reader drains makes a single buffered event spin
    forever — the socket is then never read again and every command "times out" while its
    reply sits there unread.
    """

    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, []

    def _read(self, timeout):
        self.ws.sock.settimeout(max(1, timeout))
        return json.loads(self.ws.recv())

    def call(self, method, params=None, session=None, timeout=45):
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = self._read(deadline - time.time())
            if m.get("id") == self.n:
                if "error" in m:
                    raise WSError(f"{method}: {m['error'].get('message', m['error'])}")
                return m.get("result", {})
            if "method" in m:
                self.events.append(m)
        raise TimeoutError(f"no reply to {method}")

    def wait_event(self, name, timeout=30):
        for i, m in enumerate(self.events):        # it may already have arrived
            if m.get("method") == name:
                return self.events.pop(i)
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = self._read(deadline - time.time())
            if m.get("method") == name:
                return m
            if "method" in m:
                self.events.append(m)
        raise TimeoutError(name)


def probe(ws_url=DEFAULT_WS, timeout=5):
    """Browser version string, or None when nothing answers. Cheap reachability check."""
    try:
        ws = WS(ws_url, timeout=timeout)
    except (WSError, OSError):
        return None
    try:
        v = CDP(ws).call("Browser.getVersion", timeout=timeout)
        return v.get("product") or "unknown"
    except (WSError, TimeoutError, OSError):
        return None
    finally:
        ws.close()


def wait_for_browser(ws_url=DEFAULT_WS, timeout=60, interval=2):
    """Block until the browser answers. CI starts it as a service container, so the first
    connect can lose a race with Chrome's own startup."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = probe(ws_url, timeout=4)
        if v:
            return v
        time.sleep(interval)
    return None


def render_pages(todo, ws_url=DEFAULT_WS, settle=SETTLE_SECONDS, accept_language="de-DE"):
    """todo: {key: url} -> {key: html}. Failures are omitted, like render_in_container's."""
    ws = WS(ws_url)
    cdp = CDP(ws)
    out = {}
    try:
        for key, url in todo.items():
            tid = None
            try:
                tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
                sid = cdp.call("Target.attachToTarget",
                               {"targetId": tid, "flatten": True})["sessionId"]
                cdp.call("Page.enable", session=sid)
                # German sites serve English to an unlabelled client; the old snippet passed
                # locale="de-DE" for the same reason.
                try:
                    cdp.call("Network.enable", session=sid)
                    cdp.call("Network.setExtraHTTPHeaders",
                             {"headers": {"Accept-Language": accept_language}}, session=sid)
                except WSError:
                    pass                       # not fatal: we still get the page, maybe in EN
                cdp.call("Page.navigate", {"url": url}, session=sid, timeout=45)
                try:
                    cdp.wait_event("Page.loadEventFired", timeout=30)
                except TimeoutError:
                    pass                       # slow third-party asset; the DOM is often fine
                time.sleep(settle)
                res = cdp.call("Runtime.evaluate",
                               {"expression": "document.documentElement.outerHTML",
                                "returnByValue": True}, session=sid)
                html = (res.get("result") or {}).get("value")
                if html:
                    out[key] = html[:MAX_HTML]
            except (WSError, TimeoutError, OSError) as e:
                print(f"render failed for {key}: {str(e)[:120]}", file=sys.stderr)
            finally:
                if tid:
                    try:
                        cdp.call("Target.closeTarget", {"targetId": tid}, timeout=10)
                    except (WSError, TimeoutError, OSError):
                        pass
    finally:
        ws.close()
    return out


def render(url, ws_url=DEFAULT_WS, settle=SETTLE_SECONDS):
    """One page, or raise. The single-page convenience wrapper callers actually want."""
    out = render_pages({"page": url}, ws_url=ws_url, settle=settle)
    if "page" not in out:
        raise WSError(f"render produced nothing for {url}")
    return out["page"]


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Render a page via sockpuppetbrowser over CDP")
    ap.add_argument("url", nargs="?")
    ap.add_argument("--browser-ws", default=os.environ.get("BROWSER_WS", DEFAULT_WS))
    ap.add_argument("--probe", action="store_true", help="only report whether a browser answers")
    ap.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                    help="wait up to N seconds for the browser to come up")
    ap.add_argument("--settle", type=float, default=SETTLE_SECONDS)
    ap.add_argument("--out", help="write the HTML here instead of stdout")
    args = ap.parse_args()

    if args.wait:
        v = wait_for_browser(args.browser_ws, timeout=args.wait)
    else:
        v = probe(args.browser_ws)
    if args.probe or not args.url:
        print(f"{args.browser_ws}: {v or 'no browser'}")
        return 0 if v else 1
    if not v:
        sys.exit(f"no browser at {args.browser_ws} — start one with "
                 "`docker run --rm -p 3000:3000 dgtlmoon/sockpuppetbrowser`")
    html = render(args.url, ws_url=args.browser_ws, settle=args.settle)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(html)
        print(f"wrote {len(html)} bytes to {args.out}")
    else:
        sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
