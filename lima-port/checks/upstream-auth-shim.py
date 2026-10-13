#!/usr/bin/env python3
"""A2.0 throwaway (lima-port/PLAN-SUDO.md): `nono proxy --upstream-proxy`
sends no credentials, so put this between it and an upstream proxy that
needs them. It listens on 127.0.0.1, reads each request head (CONNECT or
an absolute-URI request), adds Proxy-Authorization for the upstream, and
then copies bytes both ways.

Usage: upstream-auth-shim.py PORTFILE
  The upstream and its credentials come from $https_proxy
  (http://USER:PASS@HOST:PORT). The listening port (OS-picked) is written
  to PORTFILE.
"""

import base64
import os
import socket
import sys
import threading
import urllib.parse


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client, upstream, auth):
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = client.recv(65536)
        if not chunk:
            client.close()
            return
        head += chunk
        if len(head) > 65536:
            client.close()
            return
    first, rest = head.split(b"\r\n", 1)
    lines = [ln for ln in rest.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
             if ln and not ln.lower().startswith(b"proxy-authorization:")]
    body = rest.split(b"\r\n\r\n", 1)[1]
    new = b"\r\n".join([first, b"Proxy-Authorization: Basic " + auth] + lines) + b"\r\n\r\n"
    up = socket.create_connection(upstream)
    up.sendall(new + body)
    threading.Thread(target=pipe, args=(up, client), daemon=True).start()
    pipe(client, up)


def main():
    url = urllib.parse.urlsplit(os.environ["https_proxy"])
    upstream = (url.hostname, url.port)
    auth = base64.b64encode(f"{urllib.parse.unquote(url.username or '')}:"
                            f"{urllib.parse.unquote(url.password or '')}".encode())
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    with open(sys.argv[1], "w") as f:
        f.write(f"{srv.getsockname()[1]}\n")
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c, upstream, auth), daemon=True).start()


if __name__ == "__main__":
    main()
