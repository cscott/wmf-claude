"""Checks for bin/wmf-claude-model-proxy, the host-side model API proxy of the
contained VM. Raw requests go to its Handler over a socketpair, and the upstream
connection is a fake, so no socket file, network or token is needed.

Usage: python3 tests/test-model-proxy.py [PATH_TO_PROXY]"""
import importlib.machinery, importlib.util, json, socket, ssl, sys, threading

import os
PROXY = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "wmf-claude-model-proxy")
loader = importlib.machinery.SourceFileLoader("modelproxy", PROXY)
spec = importlib.util.spec_from_loader("modelproxy", loader)
m = importlib.util.module_from_spec(spec); loader.exec_module(m)

class FakeLog:
    def __init__(self): self.events = []
    def write(self, **e): self.events.append(e)

class FakeServer:
    token = "dummy-token"; log = FakeLog(); tls = ssl.create_default_context(); capture = None
    trip_lock = threading.Lock(); trip_logged = False
m.REQUEST_TIMEOUT = 5

# Record what would go upstream instead of connecting.
sent = {}
class FakeConn:
    def __init__(self, host, port, timeout=None, context=None): sent.clear(); sent["host"] = host; sent["headers"] = []
    def putrequest(self, method, target, **kw): sent["line"] = (method, target)
    def putheader(self, n, v): sent["headers"].append((n, v))
    def endheaders(self, body): sent["body"] = body
    def getresponse(self):
        class R:
            status, reason = 200, "OK"
            def getheaders(self): return [("Content-Type", "text/event-stream"), ("Transfer-Encoding", "chunked"), ("Set-Cookie", "x=1")]
            def __init__(self): self.chunks = [b"event: a\n\n", b"event: b\n\n"]
            def read1(self, n): return self.chunks.pop(0) if self.chunks else b""
        return R()
    def close(self): pass
m.http.client.HTTPSConnection = FakeConn

def run(raw):
    a, b = socket.socketpair()
    def serve():
        try: m.Handler(b, "test", FakeServer())
        finally: b.close()
    t = threading.Thread(target=serve); t.start()
    # The proxy may refuse and close before it reads everything; that is
    # not a test failure.
    try:
        a.sendall(raw); a.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    data = b""
    while True:
        # On Linux, the proxy can close with our request unread; the reply
        # arrives first, then a reset in place of EOF.
        try:
            c = a.recv(65536)
        except ConnectionResetError:
            break
        if not c: break
        data += c
    t.join(5); a.close()
    return data

def req(method, path, body=b"", headers=()):
    h = "".join(f"{k}: {v}\r\n" for k, v in headers)
    return f"{method} {path} HTTP/1.1\r\nHost: x\r\n{h}".encode() + b"\r\n" + body

MSG = json.dumps({"model": "m", "max_tokens": 1, "messages": []}).encode()
CL = [("Content-Length", str(len(MSG)))]
fails = 0
def check(name, raw, want):
    global fails
    out = run(raw); status = out.split(b" ", 2)[1].decode() if out else "none"
    ok = status == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}: {name}: {status} (want {want})")
    return out

check("GET /v1/models", req("GET", "/v1/models"), "403")
check("POST /v1/messages/batches", req("POST", "/v1/messages/batches", MSG, CL), "403")
check("POST /v1/messagesX", req("POST", "/v1/messagesX", MSG, CL), "403")
check("POST /v1/files", req("POST", "/v1/files", MSG, CL), "403")
check("path traversal /v1/messages/../files", req("POST", "/v1/messages/../files", MSG, CL), "403")
check("chunked", req("POST", "/v1/messages", MSG, [("Transfer-Encoding", "chunked")]), "411")
check("no Content-Length", req("POST", "/v1/messages", MSG), "411")
check("two Content-Length", req("POST", "/v1/messages", MSG, CL + CL), "400")
mcp = json.dumps({"model": "m", "max_tokens": 1, "messages": [], "mcp_servers": [{"type": "url", "url": "https://e"}]}).encode()
check("mcp_servers", req("POST", "/v1/messages", mcp, [("Content-Length", str(len(mcp)))]), "403")
ws = json.dumps({"model": "m", "max_tokens": 1, "messages": [], "tools": [{"type": "web_search_20250305", "name": "web_search"}]}).encode()
check("web_search tool", req("POST", "/v1/messages", ws, [("Content-Length", str(len(ws)))]), "403")
def body(**extra):
    d = {"model": "m", "max_tokens": 1, "messages": []}; d.update(extra)
    b = json.dumps(d).encode(); return b, [("Content-Length", str(len(b)))]
b, h = body(messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "url", "url": "https://e/x.png"}}]}])
check("image with a URL source", req("POST", "/v1/messages", b, h), "403")
b, h = body(messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [
    {"type": "document", "source": {"type": "url", "url": "https://e/d.pdf"}}]}]}])
check("URL source nested in a tool_result", req("POST", "/v1/messages", b, h), "403")
b, h = body(messages=[{"role": "user", "content": [{"type": "document", "source": {"type": "file", "file_id": "f"}}]}])
check("file source", req("POST", "/v1/messages", b, h), "403")
b, h = body(system=[{"type": "text", "text": "x"}, {"type": "image", "source": {"type": "url", "url": "https://e"}}])
check("URL source in system", req("POST", "/v1/messages", b, h), "403")
b, h = body(container="c1")
check("container", req("POST", "/v1/messages", b, h), "403")
b, h = body(attachments=[{"type": "document", "source": {"type": "url", "url": "https://e/x.pdf"}}])
out = check("URL source in a non-message field (whole-body scan)", req("POST", "/v1/messages", b, h), "403")
path_ok = b"body.attachments[0].source" in out
fails += not path_ok; print(f"{'PASS' if path_ok else 'FAIL'}: deny reason names the source path")
b, h = body(future_shaping_field="anything")
check("allowed: unknown top-level field with no fetchable source", req("POST", "/v1/messages", b, h), "200")
b, h = body(mcp_servers=[])
check("empty mcp_servers refused (key present)", req("POST", "/v1/messages", b, h), "403")
b, h = body(messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]},
                      {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "X", "input": {"source": {"type": "url"}}}]}])
check("allowed: base64 image, tool input with a 'source' key", req("POST", "/v1/messages", b, h), "200")
b, h = body(context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
            safeguards=[{"type": "auto", "classifier_context": "x"}],
            speed="fast", thread={"type": "continue", "previous_message_id": "m1"},
            diagnostics={"previous_message_id": None},
            output_config={"effort": "high", "format": {"type": "json_schema",
                "schema": {"type": "object", "properties": {"source": {"type": "string"}}}}})
check("allowed: shaping fields; a schema 'source' property is not a content source",
      req("POST", "/v1/messages", b, h), "200")
b, h = body(tools=[{"name": "cp", "input_schema": {"type": "object", "properties": {
    "source": {"type": "string"}, "dest": {"type": "string"}}}}])
check("allowed: tool input_schema with a 'source' property (schema, not content)",
      req("POST", "/v1/messages", b, h), "200")
dup = b'{"model":"m","max_tokens":1,"messages":[],"tools":[{"type":"web_search_20250305","name":"w"}],"tools":[]}'
check("duplicate key (tools)", req("POST", "/v1/messages", dup, [("Content-Length", str(len(dup)))]), "403")
deep = b'{"model":"m","max_tokens":1,"messages":' + b"[" * 100000 + b"]" * 100000 + b"}"
check("deeply nested body", req("POST", "/v1/messages", deep, [("Content-Length", str(len(deep)))]), "403")
check("non-ASCII digit Content-Length", req("POST", "/v1/messages", MSG, [("Content-Length", "\u00b2")]), "400")
check("not JSON", req("POST", "/v1/messages", b"nope", [("Content-Length", "4")]), "403")
check("HTTP/1.0", req("POST", "/v1/messages", MSG, CL).replace(b"HTTP/1.1", b"HTTP/1.0", 1), "505")
ct = json.dumps({"model": "m", "max_tokens": 1, "messages": [], "tools": [{"name": "Bash", "input_schema": {}}]}).encode()
out = check("allowed: client tool, smuggled credentials, query string",
            req("POST", "/v1/messages?beta=true", ct, [("Content-Length", str(len(ct))), ("x-api-key", "sk-smuggled"),
                ("Authorization", "Bearer smuggled"), ("Cookie", "c=1"), ("Proxy-Authorization", "p"), ("anthropic-beta", "oauth-2025-04-20")]), "200")
hdrs = {n.lower(): v for n, v in sent["headers"]}
print("  upstream:", sent["host"], sent["line"])
print("  forwarded headers:", sorted(hdrs))
ok = (hdrs.get("authorization") == "Bearer dummy-token" and "x-api-key" not in hdrs and "cookie" not in hdrs
      and "proxy-authorization" not in hdrs and hdrs.get("anthropic-beta") == "oauth-2025-04-20"
      and sent["line"] == ("POST", "/v1/messages?beta=true") and sent["body"] == ct)
fails += not ok; print(f"{'PASS' if ok else 'FAIL'}: credentials replaced, beta header kept, body exact")
resp_ok = b"event: a\n\nevent: b\n\n" in out and b"Set-Cookie" not in out and b"Transfer-Encoding" not in out and b"Connection: close" in out
fails += not resp_ok; print(f"{'PASS' if resp_ok else 'FAIL'}: streamed body relayed; Set-Cookie/Transfer-Encoding dropped; Connection: close")
# Pipelining: a second request after the first body must never be read.
sent.clear()
out = run(req("POST", "/v1/messages", MSG, CL) + req("GET", "/v1/files", b"", [("x-api-key", "k")]))
n = out.count(b"HTTP/1.1 ")
fails += n != 1; print(f"{'PASS' if n == 1 else 'FAIL'}: pipelined second request ignored ({n} response)")
# ---- Transcript capture ----
import gzip, tempfile, subprocess, shutil
BIG = 1 << 31  # a budget no test reaches

def run_on(raw, server):
    a, b = socket.socketpair()
    t = threading.Thread(target=lambda: (m.Handler(b, "t", server), b.close()))
    t.start()
    try:
        a.sendall(raw); a.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    data = b""
    while True:
        # On Linux, the proxy can close with our request unread; the reply
        # arrives first, then a reset in place of EOF.
        try:
            c = a.recv(65536)
        except ConnectionResetError:
            break
        if not c: break
        data += c
    t.join(5); a.close()
    return data

def fresh_server(path, budget=BIG):
    srv = FakeServer(); srv.log = FakeLog()
    srv.capture = m.Capture(path, budget)
    srv.trip_lock = threading.Lock(); srv.trip_logged = False
    return srv

def records(path):
    with gzip.open(path, "rt") as f:
        recs = [json.loads(line) for line in f if line.strip()]
    os.unlink(path)
    return recs

allow_body = json.dumps({"model": "m", "max_tokens": 1,
    "messages": [{"role": "user", "content": "hello-SECRET-prompt"}],
    "tools": [{"name": "Bash", "input_schema": {}}]}).encode()
allow_req = req("POST", "/v1/messages", allow_body, [("Content-Length", str(len(allow_body)))])

# An allowed turn writes a request record and a response record sharing an id;
# policy and path refusals are captured; the token is in none.
cf = tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False); cf.close()
srv = fresh_server(cf.name)
run_on(req("POST", "/v1/messages", allow_body, [("Content-Length", str(len(allow_body))),
    ("Authorization", "Bearer dummy-token")]), srv)
deny_body = json.dumps({"model": "m", "max_tokens": 1, "messages": [],
    "mcp_servers": [{"type": "url", "url": "https://e"}]}).encode()
run_on(req("POST", "/v1/messages", deny_body, [("Content-Length", str(len(deny_body)))]), srv)
run_on(req("POST", "/v1/files", b"", []), srv)
recs = records(cf.name)
reqr = [r for r in recs if r.get("decision") == "request"]
resp = [r for r in recs if r.get("decision") == "allow"]
deny = [r for r in recs if r.get("decision") == "deny"]
cap_ok = (len(reqr) == 1 and "hello-SECRET-prompt" in reqr[0].get("request", "")
          and len(resp) == 1 and "event: a" in resp[0].get("response", "")
          and reqr[0].get("id") and reqr[0]["id"] == resp[0].get("id")
          and any("mcp_servers" in d.get("request", "") for d in deny)
          and any(d.get("path") == "/v1/files" for d in deny))
fails += not cap_ok; print(f"{'PASS' if cap_ok else 'FAIL'}: request+response records share an id; policy and path refusals captured")
no_token = all("dummy-token" not in json.dumps(r) for r in recs)
fails += not no_token; print(f"{'PASS' if no_token else 'FAIL'}: the token never appears in the transcript")

# A client Accept-Encoding is forced to identity upstream, so a captured
# response is never compressed bytes.
sent.clear()
run(req("POST", "/v1/messages", allow_body, [("Content-Length", str(len(allow_body))), ("Accept-Encoding", "gzip, br")]))
hh = {n.lower(): v for n, v in sent.get("headers", [])}
enc_ok = hh.get("accept-encoding") == "identity"
fails += not enc_ok; print(f"{'PASS' if enc_ok else 'FAIL'}: the client Accept-Encoding is forced to identity upstream")

# A large refused body is captured as a bounded prefix plus a digest.
big_bad = json.dumps({"model": "m", "max_tokens": 1, "messages": [],
    "mcp_servers": [{"type": "url", "url": "https://e"}], "pad": "A" * 100000}).encode()
bf = tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False); bf.close()
srv = fresh_server(bf.name)
run_on(req("POST", "/v1/messages", big_bad, [("Content-Length", str(len(big_bad)))]), srv)
bd = [r for r in records(bf.name) if r.get("decision") == "deny"]
big_ok = (len(bd) == 1 and len(bd[0].get("request", "")) <= m.CAPTURE_DENY_REQ_MAX
          and bd[0].get("request_truncated") and len(bd[0].get("request_sha256", "")) == 64)
fails += not big_ok; print(f"{'PASS' if big_ok else 'FAIL'}: a large refused body is captured as a prefix plus sha256")

# An interrupted turn: the request record is there and the response record is
# decision=error; capture.failed stays False.
class FakeConnErr(FakeConn):
    def getresponse(self):
        class R:
            status, reason = 200, "OK"
            def getheaders(self): return [("Content-Type", "text/event-stream")]
            def __init__(self): self.n = 0
            def read1(self, n):
                self.n += 1
                if self.n == 1: return b"event: a\n\n"
                raise ConnectionResetError("peer reset")
        return R()
m.http.client.HTTPSConnection = FakeConnErr
ef = tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False); ef.close()
srv = fresh_server(ef.name)
err_body = json.dumps({"model": "m", "max_tokens": 1,
    "messages": [{"role": "user", "content": "ERR-case"}]}).encode()
run_on(req("POST", "/v1/messages", err_body, [("Content-Length", str(len(err_body)))]), srv)
failed_flag = srv.capture.failed
m.http.client.HTTPSConnection = FakeConn
erecs = records(ef.name)
ereq = [r for r in erecs if r.get("decision") == "request"]
eerr = [r for r in erecs if r.get("decision") == "error"]
err_ok = (len(ereq) == 1 and "ERR-case" in ereq[0].get("request", "")
          and len(eerr) == 1 and "event: a" in eerr[0].get("response", "")
          and ereq[0].get("id") == eerr[0].get("id") and not failed_flag)
fails += not err_ok; print(f"{'PASS' if err_ok else 'FAIL'}: interrupted turn: request record + error response record, not failed")

# F2: a request that cannot be recorded is not forwarded (503, no upstream call).
g2 = tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False); g2.close()
srv = fresh_server(g2.name); os.close(srv.capture.fd)
sent.clear()
out2 = run_on(allow_req, srv); os.unlink(g2.name)
f2_ok = (out2.split(b" ", 2)[1] == b"503" and "line" not in sent and srv.capture.failed)
fails += not f2_ok; print(f"{'PASS' if f2_ok else 'FAIL'}: a request that cannot be recorded is refused and never forwarded")

# F5: a compression failure on the response record makes no second response;
# it stops the session and the one streamed response stands.
_orig = m.gzip.compress; _cc = {"n": 0}
def _flaky(*a, **k):
    _cc["n"] += 1
    if _cc["n"] >= 2:  # 1st = request record (ok); 2nd = response record (fails)
        raise MemoryError("boom")
    return _orig(*a, **k)
m.gzip.compress = _flaky
g5 = tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False); g5.close()
srv = fresh_server(g5.name)
out5 = run_on(allow_req, srv)
m.gzip.compress = _orig; os.unlink(g5.name)
f5_ok = (out5.count(b"HTTP/1.1 ") == 1 and srv.capture.failed)
fails += not f5_ok; print(f"{'PASS' if f5_ok else 'FAIL'}: a capture compression failure makes no second response and stops the session")

# Budget: a tiny budget trips, every request 503s, and the trip is logged once.
g3 = tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False); g3.close()
srv = fresh_server(g3.name, budget=10)
sent.clear(); n0 = len(srv.log.events)
outs = [run_on(allow_req, srv) for _ in range(3)]
os.unlink(g3.name)
bud_ok = (all(o.split(b" ", 2)[1] == b"503" for o in outs) and srv.capture.failed
          and (len(srv.log.events) - n0) == 1 and "line" not in sent)
fails += not bud_ok; print(f"{'PASS' if bud_ok else 'FAIL'}: the byte budget stops the session, 503s, and logs the trip once")

# Fail closed on open: a transcript path that cannot be opened stops the
# proxy before it binds the socket (run the real script as a subprocess).
tok = tempfile.NamedTemporaryFile(delete=False); tok.write(b"sk-ant-test\n"); tok.close(); os.chmod(tok.name, 0o600)
sdir = tempfile.mkdtemp(); sockp = os.path.join(sdir, "s.sock")
r = subprocess.run([sys.executable, "-B", PROXY, "--socket", sockp,
    "--log", os.path.join(sdir, "l.log"), "--capture", "/dev/null/x.jsonl.gz",
    "--token-file", tok.name], capture_output=True, text=True, timeout=10)
os.unlink(tok.name)
bad_ok = (r.returncode != 0 and "/dev/null/x.jsonl.gz" in r.stderr and not os.path.exists(sockp))
shutil.rmtree(sdir, ignore_errors=True)
fails += not bad_ok; print(f"{'PASS' if bad_ok else 'FAIL'}: a transcript path that cannot be opened stops the proxy before the socket")

print("log decisions:", [e.get("decision") for e in FakeServer.log.events])
print("RESULT:", "ok" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
