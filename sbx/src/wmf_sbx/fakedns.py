#!/usr/bin/env python3
"""The fake-address resolver of a sandbox behind the egress proxy
(lima-port/PLAN-SUDO.md, A2.3).

Behind `restrict=on` the guest has no DNS, but a contained session's
nono looks up each name before it CONNECTs to the upstream proxy, and
fails when the lookup fails (A2.0 item 9). It does not use the address:
the proxy gets the name. So this resolver answers every A query with an
address of its own, and never asks a real resolver (one reached through
a guestfwd would be a DNS tunnel past the allowlist).

- Each new name gets the next address in FAKE_NET (198.18.0.0/15, RFC
  2544), and keeps it: the same name gets the same address, and no
  address is used twice. The mapping is an append-only file
  (`ADDRESS NAME` per line) in the sandbox's egress directory, read
  again at each start, so it lasts as long as the sandbox.
- A records only. AAAA and every other type get NODATA, so clients use
  IPv4. When the block is full, a new name gets SERVFAIL.
- DNS over TCP only (RFC 7766 framing): QEMU's guestfwd is TCP. The
  guest's glibc uses TCP at once with `options use-vc`.

It runs on the host, as a thread of the QEMU wrapper (qemu_wrapper.py),
on a Unix socket in the egress directory that the guestfwd connects to.
"""

import contextlib
import ipaddress
import os
import socket
import struct
import threading

FAKE_NET = ipaddress.ip_network("198.18.0.0/15")
TTL = 3600
MAX_NAME = 253
TYPE_A = 1
CLASS_IN = 1
# Header flags: QR, RD copied, RA; RCODEs.
QR = 0x8000
RD = 0x0100
RA = 0x0080
NOERROR, FORMERR, SERVFAIL, NOTIMP = 0, 1, 2, 4
# How long a guest connection may sit idle.
IDLE_TIMEOUT = 30


class Mapping:
    """name -> address, kept in an append-only file."""

    def __init__(self, path, net=FAKE_NET):
        self.path = path
        self.net = net
        self.lock = threading.Lock()
        self.by_name = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) != 2:
                        continue
                    with contextlib.suppress(ValueError):
                        addr = ipaddress.ip_address(parts[0])
                        if addr in net and parts[1] not in self.by_name:
                            self.by_name[parts[1]] = addr
        except FileNotFoundError:
            pass
        # The network and broadcast addresses are never handed out.
        self.next = max([int(a) for a in self.by_name.values()] + [int(net.network_address)]) + 1

    def address(self, name):
        """The address of `name`, a new one when it has none. None when the
        block is full."""
        with self.lock:
            addr = self.by_name.get(name)
            if addr is not None:
                return addr
            if self.next >= int(self.net.broadcast_address):
                return None
            addr = ipaddress.ip_address(self.next)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(f"{addr} {name}\n")
            self.next += 1
            self.by_name[name] = addr
            return addr


def parse_question(msg):
    """(id, flags, name, qtype, qclass, end of the question) of a query
    with one question. ValueError for anything else."""
    if len(msg) < 12:
        raise ValueError("short header")
    qid, flags, qd, an, ns, _ar = struct.unpack("!6H", msg[:12])
    if flags & QR or qd != 1 or an or ns:
        raise ValueError("not a query with one question")
    labels, i = [], 12
    while True:
        if i >= len(msg):
            raise ValueError("name runs off the end")
        n = msg[i]
        i += 1
        if n == 0:
            break
        if n & 0xC0:
            raise ValueError("a pointer in the question")
        label = msg[i:i + n]
        if len(label) != n:
            raise ValueError("label runs off the end")
        labels.append(label)
        i += n
    if i + 4 > len(msg):
        raise ValueError("no type and class")
    qtype, qclass = struct.unpack("!2H", msg[i:i + 4])
    try:
        name = b".".join(labels).decode("ascii").lower()
    except UnicodeDecodeError:
        raise ValueError("a name that is not ASCII") from None
    if not name or len(name) > MAX_NAME or any(c.isspace() for c in name):
        raise ValueError("a bad name")
    return qid, flags, name, qtype, qclass, i + 4


def answer(msg, mapping):
    """The response to the query `msg`, or None when it is not one."""
    try:
        qid, flags, name, qtype, qclass, end = parse_question(msg)
    except ValueError:
        if len(msg) < 2:
            return None
        return struct.pack("!6H", struct.unpack("!H", msg[:2])[0], QR | RA | FORMERR,
                           0, 0, 0, 0)
    question = msg[12:end]
    opcode = (flags >> 11) & 0xF

    def reply(rcode, records=b"", count=0):
        return (struct.pack("!6H", qid, QR | (flags & RD) | RA | rcode, 1, count, 0, 0)
                + question + records)

    if opcode != 0:
        return reply(NOTIMP)
    if qtype != TYPE_A or qclass != CLASS_IN:
        return reply(NOERROR)
    addr = mapping.address(name)
    if addr is None:
        return reply(SERVFAIL)
    # The name by a pointer to the question (offset 12).
    record = struct.pack("!HHHIH", 0xC00C, TYPE_A, CLASS_IN, TTL, 4) + addr.packed
    return reply(NOERROR, record, 1)


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


class Resolver:
    """The TCP server: one thread per guest connection, several queries
    per connection."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.sock = None

    def start(self, path):
        """Listen on the Unix socket `path` (0600; one left by an earlier
        run is replaced) and serve in a daemon thread."""
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        self.sock = socket.socket(socket.AF_UNIX)
        old = os.umask(0o177)
        try:
            self.sock.bind(path)
        finally:
            os.umask(old)
        self.sock.listen(64)
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        if self.sock:
            with contextlib.suppress(OSError):
                self.sock.close()

    def _serve(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        with client:
            client.settimeout(IDLE_TIMEOUT)
            try:
                while True:
                    head = _recv_exact(client, 2)
                    if head is None:
                        return
                    msg = _recv_exact(client, struct.unpack("!H", head)[0])
                    if msg is None:
                        return
                    out = answer(msg, self.mapping)
                    if out is None:
                        return
                    client.sendall(struct.pack("!H", len(out)) + out)
            except OSError:
                return
