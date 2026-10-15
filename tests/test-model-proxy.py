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
    token = "dummy-token"; log = FakeLog(); tls = ssl.create_default_context()
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
        c = a.recv(65536)
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
print("log decisions:", [e.get("decision") for e in FakeServer.log.events])
print("RESULT:", "ok" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
