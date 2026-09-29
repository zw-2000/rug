#!/usr/bin/env python3
"""HTTPS checks against a running stack (standard library only). Used by smoke.sh.

    smoke_check.py --host localhost --ca caddy-root.crt --expect-sha <sha256 of the v2 SOW>
"""

import argparse
import hashlib
import http.client
import json
import ssl
import sys
import time

QUESTION = "Boost Connect SR-1098 SOW - what is the scope of work mainly about?"
FAILED: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + what + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILED.append(what)


class Client:
    def __init__(self, host: str, port: int, ctx: ssl.SSLContext):
        self.host, self.port, self.ctx = host, port, ctx
        self.cookie = ""
        self.csrf = ""

    def conn(self) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(self.host, self.port, context=self.ctx, timeout=60)

    def request(self, method, path, body=None, headers=None):
        h = {"Cookie": self.cookie, **(headers or {})}
        if method != "GET":
            h["X-CSRF-Token"] = self.csrf
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c = self.conn()
        c.request(method, path, body=data, headers=h)
        return c, c.getresponse()

    def login(self, user, password, extra=None):
        c = self.conn()
        c.request(
            "POST",
            "/api/auth/login",
            body=json.dumps({"username": user, "password": password}),
            headers={"Content-Type": "application/json", **(extra or {})},
        )
        r = c.getresponse()
        body = r.read()
        set_cookie = r.getheader("Set-Cookie") or ""
        if r.status == 200:
            self.cookie = set_cookie.split(";")[0]
            self.csrf = json.loads(body)["csrf_token"]
        return r.status, set_cookie


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--http-port", type=int, default=80)
    ap.add_argument("--ca", required=True)
    ap.add_argument("--expect-sha", required=True)
    ap.add_argument("--proxy-ip", default="172.28.0.10")
    a = ap.parse_args()
    ctx = ssl.create_default_context(cafile=a.ca)

    # 1. TLS with the proxy's own CA verified (no -k), health through Caddy
    anon = Client(a.host, a.port, ctx)
    c, r = anon.request("GET", "/healthz")
    check(r.status == 200 and r.read() != b"", "healthz over verified HTTPS through Caddy")
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    check("server" not in hdrs, "Caddy hides its Server header")
    check("default-src 'self'" in hdrs.get("content-security-policy", ""), "CSP header reaches the browser")

    # 2. plain HTTP is redirected to HTTPS
    h = http.client.HTTPConnection(a.host, a.http_port, timeout=10)
    h.request("GET", "/")
    r = h.getresponse()
    check(r.status in (301, 302, 307, 308) and (r.getheader("Location") or "").startswith("https://"), "port 80 redirects to https")

    # 3. the web UI is served, and unknown API paths are JSON 404s
    c, r = anon.request("GET", "/")
    page = r.read().decode()
    check(r.status == 200 and '<div id="root">' in page, "web UI is served at /")
    c, r = anon.request("GET", "/api/definitely-not-here")
    r.read()
    check(r.status == 404 and "json" in (r.getheader("Content-Type") or ""), "unknown /api path is a JSON 404")

    # 4. sign-in: cookie flags, spoofed client address
    ann = Client(a.host, a.port, ctx)
    status, cookie = ann.login("ann", "ann-pass", {"X-Forwarded-For": "6.6.6.6"})
    low = cookie.lower()
    check(status == 200 and "secure" in low and "httponly" in low and "samesite=lax" in low, "sign-in sets a Secure, HttpOnly, SameSite cookie")
    bad, _ = Client(a.host, a.port, ctx).login("ann", "wrong-password")
    check(bad == 401, "wrong password is refused")

    # 5. streaming: the first event must arrive long before the last (nothing buffers)
    c, r = ann.request("POST", "/api/chat", {"question": QUESTION, "pinned": None})
    t0 = time.monotonic()
    seen: list[tuple[float, str]] = []
    buf = b""
    while True:
        chunk = r.read1(4096) if hasattr(r, "read1") else r.read(1)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            if block.startswith(b"event:"):
                seen.append((time.monotonic() - t0, block.split(b"\n", 1)[0].decode()[7:]))
    names = [n for _, n in seen]
    first = seen[0][0] if seen else 99
    last = seen[-1][0] if seen else 0
    check(r.status == 200 and names[-1:] == ["answer"] and "token" in names, "chat streams status, tokens and a final answer", str(names))
    check(first < 1.0 and last - first > 1.5, "events arrive incrementally through Caddy", f"first {first:.2f}s, last {last:.2f}s")

    # 6. download: the file's checksum matches the source
    final = None
    c, r = ann.request("POST", "/api/chat", {"question": QUESTION, "pinned": None})
    body = r.read().decode()
    for block in body.split("\n\n"):
        if block.startswith("event: answer"):
            final = json.loads(block.split("data: ", 1)[1])
    src = (final or {}).get("sources", [{}])[0]
    check(bool(src) and src.get("filename", "").endswith("v2 FINAL.docx"), "the cited source is the v2 FINAL SOW")
    c, r = ann.request("GET", f"/api/documents/{src.get('id', 'x')}/download")
    data = r.read()
    check(r.status == 200 and "attachment" in (r.getheader("Content-Disposition") or ""), "download is an attachment")
    check(hashlib.sha256(data).hexdigest() == a.expect_sha, "downloaded file matches the source checksum")

    # 7. the audit log records the real client, not the proxy or a spoofed header
    root = Client(a.host, a.port, ctx)
    root.login("root", "root-pass")
    c, r = root.request("GET", "/api/admin/audit?action=login.ok&actor=ann")
    rows = json.loads(r.read())
    ip = rows[0]["ip"] if rows else None
    check(bool(ip) and ip != a.proxy_ip and ip != "6.6.6.6", "audit records the client address (not Caddy, not a spoofed header)", str(ip))

    print("\nFAILED: " + ", ".join(FAILED) if FAILED else "\nall HTTPS checks passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
