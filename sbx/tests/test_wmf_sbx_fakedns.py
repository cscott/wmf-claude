#!/usr/bin/env python3
"""Unit tests for the fake-address resolver (fakedns.py,
README.md, Architecture) -- run with:
  python3 -m unittest discover -s sbx/tests -v"""

import ipaddress
import os
import socket
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.fakedns as fakedns  # noqa: E402
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


def query(name, qtype=fakedns.TYPE_A, qid=0x1234, flags=0x0100):
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return struct.pack("!6H", qid, flags, 1, 0, 0, 0) + q + struct.pack("!2H", qtype, 1)


def parse(resp):
    qid, flags, qd, an, _ns, _ar = struct.unpack("!6H", resp[:12])
    addr = None
    if an:
        addr = ipaddress.ip_address(resp[-4:])
    return qid, flags & 0xF, an, addr, flags


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "names")

    def test_names_get_increasing_addresses_and_keep_them(self):
        m = fakedns.Mapping(self.path)
        a, b = m.address("a.example"), m.address("b.example")
        self.assertEqual(str(a), "198.18.0.1")
        self.assertEqual(str(b), "198.18.0.2")
        self.assertEqual(m.address("a.example"), a)
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    def test_the_mapping_outlives_a_restart(self):
        m = fakedns.Mapping(self.path)
        m.address("a.example")
        m.address("b.example")
        again = fakedns.Mapping(self.path)
        self.assertEqual(str(again.address("b.example")), "198.18.0.2")
        self.assertEqual(str(again.address("c.example")), "198.18.0.3")
        with open(self.path) as f:
            self.assertEqual(f.read().split("\n")[:3],
                             ["198.18.0.1 a.example", "198.18.0.2 b.example",
                              "198.18.0.3 c.example"])

    def test_junk_lines_and_addresses_out_of_the_block_are_skipped(self):
        with open(self.path, "w") as f:
            f.write("garbage\n10.0.0.1 evil.example\n198.18.0.9 ok.example\n")
        m = fakedns.Mapping(self.path)
        self.assertEqual(str(m.address("ok.example")), "198.18.0.9")
        self.assertEqual(str(m.address("evil.example")), "198.18.0.10")

    def test_a_full_block_gives_no_address(self):
        m = fakedns.Mapping(self.path, net=ipaddress.ip_network("198.18.0.0/30"))
        self.assertEqual(str(m.address("a")), "198.18.0.1")
        self.assertEqual(str(m.address("b")), "198.18.0.2")
        self.assertIsNone(m.address("c"))


class AnswerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = fakedns.Mapping(os.path.join(self.tmp.name, "names"))

    def test_an_a_query_gets_its_address(self):
        qid, rcode, an, addr, flags = parse(fakedns.answer(query("WWW.Example.org"), self.m))
        self.assertEqual((qid, rcode, an, str(addr)), (0x1234, 0, 1, "198.18.0.1"))
        self.assertTrue(flags & fakedns.QR and flags & fakedns.RD)
        self.assertEqual(self.m.by_name, {"www.example.org": addr})

    def test_aaaa_gets_nodata(self):
        _qid, rcode, an, _addr, _f = parse(fakedns.answer(query("a.example", qtype=28), self.m))
        self.assertEqual((rcode, an), (0, 0))
        self.assertEqual(self.m.by_name, {})

    def test_a_response_or_junk_gets_formerr(self):
        resp = fakedns.answer(query("a.example", flags=0x8000), self.m)
        self.assertEqual(parse(resp)[1], fakedns.FORMERR)
        self.assertEqual(parse(fakedns.answer(b"\x12\x34junk", self.m))[1], fakedns.FORMERR)
        self.assertIsNone(fakedns.answer(b"\x12", self.m))

    def test_a_pointer_or_a_long_label_run_is_refused(self):
        bad = struct.pack("!6H", 1, 0, 1, 0, 0, 0) + b"\xc0\x0c" + struct.pack("!2H", 1, 1)
        self.assertEqual(parse(fakedns.answer(bad, self.m))[1], fakedns.FORMERR)
        bad = struct.pack("!6H", 1, 0, 1, 0, 0, 0) + b"\x3fabc"
        self.assertEqual(parse(fakedns.answer(bad, self.m))[1], fakedns.FORMERR)

    def test_another_opcode_is_not_implemented(self):
        resp = fakedns.answer(query("a.example", flags=0x1000), self.m)
        self.assertEqual(parse(resp)[1], fakedns.NOTIMP)


class ResolverTests(unittest.TestCase):
    def test_queries_over_one_tcp_connection(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "dns.sock")
        r = fakedns.Resolver(fakedns.Mapping(os.path.join(tmp.name, "names")))
        r.start(path)
        self.addCleanup(r.close)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(path)
            for name, want in (("a.example", "198.18.0.1"), ("b.example", "198.18.0.2")):
                q = query(name)
                s.sendall(struct.pack("!H", len(q)) + q)
                n = struct.unpack("!H", fakedns._recv_exact(s, 2))[0]
                self.assertEqual(str(parse(fakedns._recv_exact(s, n))[3]), want)


if __name__ == "__main__":
    unittest.main()
