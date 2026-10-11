#!/usr/bin/env python3
"""Track L, step L1: a prototype host-side credential proxy (HANDOFF-LIMA.md,
track L, option H). Not the product: a measuring tool.

It listens on the host's loopback. A Lima guest reaches it at
192.168.5.2:PORT. Claude Code in the guest gets

  ANTHROPIC_BASE_URL=http://192.168.5.2:PORT
  ANTHROPIC_API_KEY=<phantom>          (API-key mode)
  or CLAUDE_CODE_OAUTH_TOKEN=<phantom> (OAuth mode)

The proxy refuses a request that does not carry the phantom, puts the real
credential in its place, and forwards the request to api.anthropic.com,
streaming the response. It logs one line per request (method, path,
status, bytes, seconds, which auth headers came in) to stderr, and never
logs a credential or a body.

The real credential comes from the proxy's own environment:
  ANTHROPIC_API_KEY          -> x-api-key
  CLAUDE_CODE_OAUTH_TOKEN    -> Authorization: Bearer
  --oauth-file FILE          -> Authorization: Bearer (the first line; read
                                per request, so a refreshed file is used)
  --claude-credentials FILE  -> Authorization: Bearer from Claude Code's
                                ~/.claude/.credentials.json
                                (claudeAiOauth.accessToken; read per request)

Usage:
  credential-proxy.py [--port N] [--phantom STRING] [--oauth-file F |
                       --claude-credentials F]
It prints the port and the phantom on stdout, then serves. A host HTTPS
proxy in $HTTPS_PROXY is used for the upstream connection.
"""

import argparse
import base64
import http.client
import http.server
import json
import os
import secrets
import socketserver
import sys
import time
import urllib.parse

UPSTREAM = "api.anthropic.com"
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
AUTH_HEADERS = ("x-api-key", "authorization")
# Claude Code's connectivity probe: no credential, so forward it without one.
OPEN_PATHS = {("HEAD", "/api/hello"), ("GET", "/api/hello")}


def real_credential(args):
    """(header, value) of the real credential, read now."""
    if args.claude_credentials:
        with open(args.claude_credentials, encoding="utf-8") as f:
            token = json.load(f)["claudeAiOauth"]["accessToken"]
        return "authorization", f"Bearer {token}"
    if args.oauth_file:
        with open(args.oauth_file, encoding="utf-8") as f:
            return "authorization", "Bearer " + f.readline().strip()
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "authorization", "Bearer " + os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "x-api-key", os.environ["ANTHROPIC_API_KEY"]
    raise SystemExit("credential-proxy: no credential (ANTHROPIC_API_KEY, "
                     "CLAUDE_CODE_OAUTH_TOKEN, --oauth-file or --claude-credentials)")


def upstream_connection():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return http.client.HTTPSConnection(UPSTREAM, 443, timeout=600)
    # HTTPSConnection plus set_tunnel does a plain-HTTP CONNECT to the
    # proxy, then TLS to UPSTREAM through the tunnel.
    p = urllib.parse.urlsplit(proxy)
    headers = {}
    if p.username:
        cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
    conn = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=600)
    conn.set_tunnel(UPSTREAM, 443, headers=headers)
    return conn


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    args = None

    def log_message(self, *a):
        pass

    def _serve(self):
        start = time.monotonic()
        came = [h for h in AUTH_HEADERS if self.headers.get(h)]
        presented = [self.headers.get(h, "") for h in came]
        phantom = self.args.phantom
        open_path = (self.command, self.path.split("?")[0]) in OPEN_PATHS
        if not open_path and not any(v == phantom or v == f"Bearer {phantom}"
                                     for v in presented):
            self._refuse(403, "no phantom credential", came, start)
            return
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() not in AUTH_HEADERS}
        if not open_path:
            hname, hvalue = real_credential(self.args)
            headers[hname] = hvalue
        headers["host"] = UPSTREAM
        conn = upstream_connection()
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            if self.command == "HEAD":
                self.end_headers()
                self._log(resp.status, 0, came, start)
                return
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            sent = 0
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                sent += len(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self._log(resp.status, sent, came, start)
        finally:
            conn.close()

    def _refuse(self, status, why, came, start):
        msg = json.dumps({"error": why}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)
        self._log(status, len(msg), came, start)

    def _log(self, status, size, came, start):
        print(f"{time.strftime('%H:%M:%S')} {self.command} {self.path.split('?')[0]} "
              f"{status} {size}B {time.monotonic() - start:.1f}s auth-in={','.join(came) or '-'}",
              file=sys.stderr, flush=True)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _serve


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--phantom", default="wmfsbx-phantom-" + secrets.token_hex(16))
    p.add_argument("--oauth-file")
    p.add_argument("--claude-credentials")
    args = p.parse_args()
    real_credential(args)  # fail early
    Handler.args = args
    srv = Server(("127.0.0.1", args.port), Handler)
    print(f"port {srv.server_address[1]}\nphantom {args.phantom}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
