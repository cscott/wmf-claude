#!/usr/bin/env python3
"""Unit tests for bin/wmf-sbx-mcp-proxy -- run with:
  python3 -m unittest discover -s sbx/tests -v

The proxy's whole job is a translation, so the tests drive it against a
fake gateway that reproduces the parts of sbx's that matter: streamable
HTTP with SSE-framed replies, an `Mcp-Session-Id` handed out at
initialize and required afterwards, the five meta-tools, and a server
whose tools only appear once `mcp-add` has mounted it.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN = os.path.join(os.path.dirname(__file__), "..", "helpers", "wmf-sbx-mcp-proxy")


def _load_proxy_module():
    # The script has no .py extension, so name a loader explicitly.
    loader = importlib.machinery.SourceFileLoader("wmf_sbx_mcp_proxy", BIN)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


proxy_mod = _load_proxy_module()

META = [
    {"name": n, "description": "meta", "inputSchema": {"type": "object"}}
    for n in ("mcp-find", "mcp-add", "mcp-config-set", "mcp-exec", "code-mode")
]
PHAB_TOOLS = [
    {"name": "phabricator_get_task", "description": "get a task"},
    {"name": "phabricator_search_tasks", "description": "search tasks"},
]
GERRIT_TOOLS = [{"name": "get_commit_message", "description": "commit message"}]


class FakeGatewayState:
    """What the fake gateway did, so the tests can assert on it."""

    def __init__(self, mounted=(), serve_meta=True, plain_json=False):
        self.mounted = list(mounted)
        self.serve_meta = serve_meta
        self.plain_json = plain_json
        self.session_id = None    # issued by the first `initialize`
        self.live = set()         # ids we still answer for
        self.serial = 0
        self.calls = []           # (method, params, session-id header)
        self.tool_calls = []      # tools/call names
        self.sessionless = []     # requests that forgot the session header

    def new_session(self):
        """Issue the next session id, as a real `initialize` does."""
        self.serial += 1
        self.session_id = "SESSION-%d" % self.serial
        self.live.add(self.session_id)
        return self.session_id

    def expire_session(self):
        """Forget the current session without telling anyone -- an idle
        timeout, or a gateway restart. Every later request carrying that id
        gets the spec's 404."""
        self.live.discard(self.session_id)

    def restart(self):
        """Harsher: the session is gone *and* so are the mounts."""
        self.expire_session()
        self.mounted = []

    def mount(self, name):
        """Mounting is idempotent: the real gateway's second `mcp-add` for
        one server adds no second copy of its tools."""
        if name not in self.mounted:
            self.mounted.append(name)
            return True
        return False


class FakeGatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output clean
        pass

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        msg = json.loads(self.rfile.read(length) or "{}")
        if self.path != "/mcp":
            # A 404 that is *not* a session expiry -- the other thing the
            # same status code means, and the reason the proxy only reads
            # it as an expiry when it sent a session id.
            self._not_found()
            return
        method = msg.get("method")
        sid = self.headers.get("Mcp-Session-Id")
        state.calls.append((method, msg.get("params"), sid))
        if method != "initialize" and not sid:
            state.sessionless.append(method)
        if sid and sid not in state.live:
            # MCP streamable HTTP: a server that has dropped a session
            # answers 404, and the client is expected to re-`initialize`.
            self._not_found()
            return

        if msg.get("id") is None:  # a notification
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-gateway", "version": "0"},
            }
            self._reply(msg["id"], result, session_id=state.new_session())
            return
        if method == "tools/list":
            tools = list(META) if state.serve_meta else []
            for name in state.mounted:
                tools.extend(PHAB_TOOLS if name == "phabricator" else GERRIT_TOOLS)
            self._reply(msg["id"], {"tools": tools})
            return
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            state.tool_calls.append(name)
            if name == "mcp-add":
                server = params["arguments"]["name"]
                added = state.mount(server)
                # The real gateway answers with a count and no names
                # (MEASURED, NOTES.md §62.2) -- which is why the proxy
                # diffs `tools/list` instead of parsing this.
                text = 'Added "%s" with %d tools.' % (
                    server,
                    len(PHAB_TOOLS if server == "phabricator" else GERRIT_TOOLS)
                    if added
                    else 0,
                )
                self._reply(msg["id"], {"content": [{"type": "text", "text": text}]})
                return
            self._reply(
                msg["id"],
                {"content": [{"type": "text", "text": "called %s" % name}]},
            )
            return
        self._reply_error(msg["id"], -32601, "method not found")

    def _write(self, payload, session_id=None):
        state = self.server.state
        if state.plain_json:
            body = json.dumps(payload).encode()
            ctype = "application/json"
        else:
            body = ("event: message\nid: 1\ndata: %s\n\n" % json.dumps(payload)).encode()
            ctype = "text/event-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(body)

    def _reply(self, msg_id, result, session_id=None):
        self._write({"jsonrpc": "2.0", "id": msg_id, "result": result}, session_id)

    def _not_found(self):
        body = b'{"error": "session not found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply_error(self, msg_id, code, message):
        self._write(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
        )


class ProxyTestCase(unittest.TestCase):
    """Base: a fake gateway on a loopback port, torn down after each test."""

    mounted = ()
    serve_meta = True
    plain_json = False

    def setUp(self):
        self.state = FakeGatewayState(
            mounted=self.mounted,
            serve_meta=self.serve_meta,
            plain_json=self.plain_json,
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGatewayHandler)
        self.httpd.state = self.state
        self.url = "http://127.0.0.1:%d/mcp" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        # The proxy logs to stderr by design; capture it instead of
        # scrolling it past the test results.
        self.stderr = io.StringIO()
        real_stderr, sys.stderr = sys.stderr, self.stderr
        self.addCleanup(setattr, sys, "stderr", real_stderr)

    # What a phabricator proxy would be given in the kit. Spelled out
    # because `--tools` is mandatory now (the gateway's namespace is flat),
    # so "the default tool set" is not a thing any more.
    PHAB_ALLOW = "phabricator_get_task,phabricator_search_tasks"

    def make_proxy(self, server="phabricator", tools=PHAB_ALLOW, no_add=False, url=None):
        """Build a Proxy through `parse_args`, so the tests exercise the
        CLI contract. `tools=None` bypasses it deliberately: the CLI will
        not let you omit the allowlist, so that is the only way to reach
        the library-level fail-closed backstop."""
        if tools is None:
            gateway = proxy_mod.Gateway(url or self.url, "Bearer test")
            return proxy_mod.Proxy(server, gateway, allowlist=None, try_add=not no_add)
        argv = [server, "--tools", tools]
        if no_add:
            argv.append("--no-add")
        argv += ["--url", url or self.url]
        args = proxy_mod.parse_args(argv)
        return proxy_mod.Proxy(
            args.server,
            proxy_mod.Gateway(args.url, "Bearer test"),
            allowlist=args.tools,
            try_add=not args.no_add,
        )

    @staticmethod
    def request(proxy, method, params=None, msg_id=1):
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        return proxy.handle(msg)


class InitializeTests(ProxyTestCase):
    def test_serverinfo_name_is_the_server_name(self):
        """This is the whole point: Claude derives `mcp__<name>__<tool>`
        from the config entry, and the entry is named after the server."""
        reply = self.request(self.make_proxy("phabricator"), "initialize", {})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "phabricator")

    def test_protocol_version_echoes_the_client(self):
        reply = self.request(
            self.make_proxy(), "initialize", {"protocolVersion": "2024-11-05"}
        )
        self.assertEqual(reply["result"]["protocolVersion"], "2024-11-05")

    def test_only_tools_capability_is_advertised(self):
        reply = self.request(self.make_proxy(), "initialize", {})
        self.assertEqual(list(reply["result"]["capabilities"]), ["tools"])

    def test_handshake_completes_before_any_tools_list(self):
        proxy = self.make_proxy()
        self.request(proxy, "initialize", {})
        methods = [c[0] for c in self.state.calls]
        self.assertEqual(methods[:2], ["initialize", "notifications/initialized"])
        self.assertEqual(self.state.sessionless, [])

    def test_a_dead_gateway_does_not_fail_the_handshake(self):
        """A session that dies at initialize takes the whole agent's MCP
        config with it; an empty tool list is recoverable."""
        proxy = self.make_proxy(url="http://127.0.0.1:1/mcp")
        reply = self.request(proxy, "initialize", {})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "phabricator")
        self.assertFalse(proxy.connected)

    def test_a_dead_gateway_reports_an_error_on_tools_list(self):
        proxy = self.make_proxy(url="http://127.0.0.1:1/mcp")
        reply = self.request(proxy, "tools/list")
        self.assertEqual(reply["error"]["code"], -32603)


class ToolListingTests(ProxyTestCase):
    def test_meta_tools_are_never_exposed(self):
        reply = self.request(self.make_proxy(), "tools/list")
        names = {t["name"] for t in reply["result"]["tools"]}
        self.assertFalse(names & proxy_mod.META_TOOLS)

    def test_mcp_add_mounts_the_server_and_its_tools_appear(self):
        reply = self.request(self.make_proxy("phabricator"), "tools/list")
        names = sorted(t["name"] for t in reply["result"]["tools"])
        self.assertEqual(names, ["phabricator_get_task", "phabricator_search_tasks"])
        self.assertIn("mcp-add", self.state.tool_calls)

    def test_descriptions_and_schemas_pass_through_untouched(self):
        reply = self.request(self.make_proxy(), "tools/list")
        tool = [t for t in reply["result"]["tools"] if t["name"] == "phabricator_get_task"]
        self.assertEqual(tool[0]["description"], "get a task")

    def test_allowlist_filters_the_gateway_set(self):
        proxy = self.make_proxy(tools="phabricator_get_task")
        reply = self.request(proxy, "tools/list")
        self.assertEqual(
            [t["name"] for t in reply["result"]["tools"]], ["phabricator_get_task"]
        )

    def test_allowlist_warns_about_tools_the_gateway_does_not_serve(self):
        proxy = self.make_proxy(tools="phabricator_get_task,phabricator_retired")
        self.request(proxy, "tools/list")
        self.assertIn("phabricator_retired", self.stderr.getvalue())

    def test_plain_json_replies_parse_too(self):
        """The spec allows either framing; sbx uses SSE, so don't assume."""
        self.state.plain_json = True
        reply = self.request(self.make_proxy(), "tools/list")
        self.assertTrue(reply["result"]["tools"])


class StaticModeTests(ProxyTestCase):
    """`sbx create --static-mcp <server>` mounts the server up front, and
    serves no `mcp-add` -- so there is no diff to attribute tools with and
    `--tools` is the only thing that can say which tools are ours."""

    mounted = ("phabricator",)
    serve_meta = False

    def test_no_add_with_an_allowlist_serves_exactly_it(self):
        proxy = self.make_proxy(
            "phabricator",
            tools="phabricator_get_task,phabricator_search_tasks",
            no_add=True,
        )
        reply = self.request(proxy, "tools/list")
        self.assertEqual(len(reply["result"]["tools"]), 2)
        self.assertEqual(self.state.tool_calls, [])

    def test_a_gateway_without_mcp_add_is_not_an_error(self):
        proxy = self.make_proxy(  # --no-add NOT passed: there is no mcp-add to call
            "phabricator", tools="phabricator_get_task"
        )
        reply = self.request(proxy, "tools/list")
        self.assertEqual(len(reply["result"]["tools"]), 1)
        self.assertEqual(self.state.tool_calls, [])

    def test_without_an_allowlist_it_fails_closed(self):
        """The flat namespace gives no attribution (NOTES.md §62.2), so
        exposing everything would answer to another server's tool names
        under ours. Expose nothing, loudly."""
        proxy = self.make_proxy("phabricator", tools=None, no_add=True)
        reply = self.request(proxy, "tools/list")
        self.assertEqual(reply["result"]["tools"], [])
        self.assertIn("--tools", self.stderr.getvalue())

    def test_failing_closed_also_refuses_calls(self):
        proxy = self.make_proxy("phabricator", tools=None, no_add=True)
        reply = self.request(
            proxy, "tools/call", {"name": "phabricator_get_task", "arguments": {}}
        )
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertEqual(self.state.tool_calls, [])


class MultiServerTests(ProxyTestCase):
    """Two proxies, one gateway serving both servers. The gateway's
    `tools/list` is one flat namespace with no attribution, so each proxy
    must keep to its own tools by allowlist or by the `mcp-add` diff."""

    mounted = ("phabricator", "gerrit")
    serve_meta = False

    def test_allowlist_separates_two_servers_on_one_gateway(self):
        phab = self.make_proxy("phabricator", tools="phabricator_get_task", no_add=True)
        gerrit = self.make_proxy("gerrit", tools="get_commit_message", no_add=True)
        self.assertEqual(
            [t["name"] for t in self.request(phab, "tools/list")["result"]["tools"]],
            ["phabricator_get_task"],
        )
        self.assertEqual(
            [t["name"] for t in self.request(gerrit, "tools/list")["result"]["tools"]],
            ["get_commit_message"],
        )

    def test_a_foreign_tool_on_the_allowlist_is_not_exposed(self):
        """Measured hazard: with both servers mounted, nothing stops a
        typo'd or copy-pasted allowlist from re-exporting the other
        server's tool as `mcp__phabricator__get_commit_message`."""
        phab = self.make_proxy(
            "phabricator", tools="phabricator_get_task,get_commit_message", no_add=True
        )
        names = [t["name"] for t in self.request(phab, "tools/list")["result"]["tools"]]
        self.assertEqual(names, ["phabricator_get_task", "get_commit_message"])
        # ...which is why static mode's allowlist is a policy statement the
        # kit owns. In dynamic mode the mcp-add diff catches it: see
        # DynamicAttributionTests.


class DynamicAttributionTests(ProxyTestCase):
    """Dynamic mode: the gateway serves `mcp-add`, so for the one moment
    of a *first* mount the proxy can see which tools appeared and were
    therefore ours. That is a cross-check on `--tools`, not a substitute
    for it -- mounts are sandbox-wide and outlive the session (§62.3), so
    on every later start the diff is empty."""

    mounted = ()
    serve_meta = True

    def test_the_mcp_add_diff_records_what_this_server_serves(self):
        proxy = self.make_proxy("phabricator")
        reply = self.request(proxy, "tools/list")
        self.assertEqual(
            sorted(t["name"] for t in reply["result"]["tools"]),
            ["phabricator_get_task", "phabricator_search_tasks"],
        )
        self.assertEqual(proxy.discovered,
                         frozenset({"phabricator_get_task", "phabricator_search_tasks"}))

    def test_a_server_mounted_by_someone_else_does_not_leak_in(self):
        """A second server is already mounted when we start: its tools are
        in the before-snapshot, so the diff excludes them."""
        self.state.mount("gerrit")
        proxy = self.make_proxy("phabricator")
        names = sorted(t["name"] for t in self.request(proxy, "tools/list")["result"]["tools"])
        self.assertEqual(names, ["phabricator_get_task", "phabricator_search_tasks"])

    def test_the_diff_overrides_a_foreign_allowlist_entry(self):
        self.state.mount("gerrit")
        proxy = self.make_proxy(
            "phabricator", tools="phabricator_get_task,get_commit_message"
        )
        names = [t["name"] for t in self.request(proxy, "tools/list")["result"]["tools"]]
        self.assertEqual(names, ["phabricator_get_task"])
        self.assertIn("does not serve", self.stderr.getvalue())

    def test_an_already_mounted_server_still_serves_its_allowlist(self):
        """The common case after the first sandbox start: `mcp-add` adds
        nothing, so there is no diff -- and the allowlist carries it."""
        self.state.mount("phabricator")
        proxy = self.make_proxy("phabricator")
        names = sorted(t["name"] for t in self.request(proxy, "tools/list")["result"]["tools"])
        self.assertEqual(names, ["phabricator_get_task", "phabricator_search_tasks"])
        self.assertIsNone(proxy.discovered)
        self.assertIn("added no tools", self.stderr.getvalue())

    def test_the_diff_is_not_a_discovery_mechanism(self):
        """Even when the diff *does* attribute tools, no allowlist still
        means no tools: relying on the diff would serve the full set once
        and nothing ever after."""
        proxy = self.make_proxy("phabricator", tools=None)
        reply = self.request(proxy, "tools/list")
        self.assertEqual(reply["result"]["tools"], [])
        self.assertEqual(proxy.discovered,
                         frozenset({"phabricator_get_task", "phabricator_search_tasks"}))
        self.assertIn("--tools", self.stderr.getvalue())


class ToolCallTests(ProxyTestCase):
    def test_a_call_is_forwarded_verbatim(self):
        proxy = self.make_proxy()
        self.request(proxy, "tools/list")
        reply = self.request(
            proxy, "tools/call", {"name": "phabricator_get_task", "arguments": {"id": 1}}
        )
        self.assertEqual(
            reply["result"]["content"][0]["text"], "called phabricator_get_task"
        )
        forwarded = [c for c in self.state.calls if c[0] == "tools/call"][-1]
        self.assertEqual(forwarded[1], {"name": "phabricator_get_task", "arguments": {"id": 1}})
        self.assertEqual(forwarded[2], self.state.session_id)

    def test_a_call_works_without_a_prior_tools_list(self):
        """Claude lists before calling, but a resumed session may not."""
        reply = self.request(
            self.make_proxy(), "tools/call", {"name": "phabricator_get_task"}
        )
        self.assertIn("result", reply)

    def test_an_unknown_tool_is_refused_not_forwarded(self):
        proxy = self.make_proxy()
        reply = self.request(proxy, "tools/call", {"name": "rm_minus_rf"})
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertNotIn("rm_minus_rf", self.state.tool_calls)

    def test_a_tool_outside_the_allowlist_is_refused(self):
        proxy = self.make_proxy(tools="phabricator_get_task")
        reply = self.request(
            proxy, "tools/call", {"name": "phabricator_search_tasks"}
        )
        self.assertIn("error", reply)
        self.assertNotIn("phabricator_search_tasks", self.state.tool_calls)

    def test_the_gateway_meta_tools_are_not_callable_through_us(self):
        proxy = self.make_proxy()
        reply = self.request(proxy, "tools/call", {"name": "code-mode"})
        self.assertIn("error", reply)
        self.assertNotIn("code-mode", self.state.tool_calls)


class SessionExpiryTests(ProxyTestCase):
    """A gateway that forgets our session must not take the proxy with it.

    MEASURED in a live sandbox (NOTES.md §72.2): both servers showed
    "✔ Connected" and every `tools/call` came back
    `HTTP 404: session not found`. The proxy captured `Mcp-Session-Id`
    once and kept echoing it forever, and 404 is exactly how streamable
    HTTP says a session has gone away.
    """

    def test_a_call_survives_an_expired_session(self):
        proxy = self.make_proxy()
        self.request(proxy, "tools/list")
        dead = self.state.session_id
        self.state.expire_session()
        reply = self.request(
            proxy, "tools/call", {"name": "phabricator_get_task", "arguments": {}}
        )
        self.assertIn("result", reply, reply)
        self.assertEqual(
            reply["result"]["content"][0]["text"], "called phabricator_get_task"
        )
        # A new session, and the retry carried it.
        self.assertNotEqual(self.state.session_id, dead)
        self.assertEqual(proxy.gw.session_id, self.state.session_id)
        forwarded = [c for c in self.state.calls if c[0] == "tools/call"][-1]
        self.assertEqual(forwarded[2], self.state.session_id)
        self.assertEqual(proxy.gw.reconnects, 1)

    def test_a_listing_survives_an_expired_session(self):
        proxy = self.make_proxy()
        self.request(proxy, "tools/list")
        self.state.expire_session()
        names = sorted(
            t["name"] for t in self.request(proxy, "tools/list")["result"]["tools"]
        )
        self.assertEqual(names, ["phabricator_get_task", "phabricator_search_tasks"])

    def test_a_restarted_gateway_is_re_mounted_before_the_retry(self):
        """Session expiry usually leaves the mount (§62.3). A restart does
        not, and then the retried call would name a tool the gateway no
        longer serves -- so the reconnect re-runs `mcp-add` first."""
        proxy = self.make_proxy()
        self.request(proxy, "tools/list")
        self.assertEqual(self.state.mounted, ["phabricator"])
        self.state.restart()
        reply = self.request(
            proxy, "tools/call", {"name": "phabricator_get_task", "arguments": {}}
        )
        self.assertIn("result", reply, reply)
        self.assertEqual(self.state.mounted, ["phabricator"])

    def test_it_does_not_re_handshake_forever(self):
        """A gateway that 404s every session must produce an error, not a
        recursion. `initialize` is never the thing we retry."""
        proxy = self.make_proxy()
        self.request(proxy, "tools/list")

        original = self.state.new_session

        def expire_immediately():
            sid = original()
            self.state.live.discard(sid)
            return sid

        self.state.new_session = expire_immediately
        self.state.expire_session()  # and the one we are holding
        reply = self.request(proxy, "tools/call", {"name": "phabricator_get_task"})
        self.assertIn("error", reply)
        self.assertLessEqual(
            len([c for c in self.state.calls if c[0] == "initialize"]), 3
        )

    def test_the_error_does_not_blame_credentials(self):
        """The message a failed call hands the model matters: the first one
        said only `HTTP 404: session not found`, and the model went looking
        for a host-side credential problem that did not exist."""
        gateway = proxy_mod.Gateway(self.url, "Bearer test")
        gateway.connect()
        self.state.expire_session()
        with self.assertRaises(proxy_mod.GatewaySessionLost) as caught:
            gateway.call("tools/list", {}, retry=False)
        message = str(caught.exception)
        self.assertIn("session expiry", message)
        self.assertIn("not an authentication failure", message)

    def test_a_404_before_the_handshake_is_not_an_expiry(self):
        """No session id was sent, so 404 means the URL is wrong -- and
        re-handshaking against a wrong URL would just loop."""
        gateway = proxy_mod.Gateway(self.url, "Bearer test")
        gateway.session_id = None
        self.state.live.clear()
        # An id the gateway never issued is still an id *we* sent; the
        # honest no-session case is the plain URL being unserved.
        with self.assertRaises(proxy_mod.GatewayError) as caught:
            proxy_mod.Gateway(self.url + "/nope", "Bearer test").connect()
        self.assertNotIsInstance(caught.exception, proxy_mod.GatewaySessionLost)


class ProtocolTests(ProxyTestCase):
    def test_notifications_get_no_reply(self):
        proxy = self.make_proxy()
        self.assertIsNone(proxy.handle({"jsonrpc": "2.0", "method": "notifications/x"}))

    def test_ping_is_answered_without_touching_the_gateway(self):
        proxy = self.make_proxy()
        self.assertEqual(self.request(proxy, "ping")["result"], {})
        self.assertEqual(self.state.calls, [])

    def test_unknown_methods_get_method_not_found(self):
        reply = self.request(self.make_proxy(), "completion/complete")
        self.assertEqual(reply["error"]["code"], -32601)

    def test_resources_and_prompts_answer_empty(self):
        proxy = self.make_proxy()
        self.assertEqual(self.request(proxy, "resources/list")["result"], {"resources": []})
        self.assertEqual(self.request(proxy, "prompts/list")["result"], {"prompts": []})

    def test_serve_loop_reads_lines_and_writes_replies(self):
        proxy = self.make_proxy()
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
            + "\n"  # blank lines are skipped
            + "not json\n"  # and so is garbage
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
            + "\n"
        )
        stdout = io.StringIO()
        proxy_mod.serve(proxy, stdin, stdout)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertIn("unparseable", self.stderr.getvalue())


class ArgumentTests(unittest.TestCase):
    def setUp(self):
        # argparse prints usage to stderr before exiting; keep it out of
        # the test output.
        real_stderr, sys.stderr = sys.stderr, io.StringIO()
        self.addCleanup(setattr, sys, "stderr", real_stderr)

    def test_tools_parses_to_a_set(self):
        args = proxy_mod.parse_args(["gerrit", "--tools", "a, b ,c,"])
        self.assertEqual(args.tools, frozenset({"a", "b", "c"}))

    def test_tools_is_required(self):
        """Nothing else can say which of the gateway's flat list of tools
        belong to this server (NOTES.md §62.2), so the CLI will not start
        without one."""
        with self.assertRaises(SystemExit):
            proxy_mod.parse_args(["gerrit"])

    def test_url_defaults_to_the_env_then_the_well_known_gateway(self):
        self.assertEqual(
            proxy_mod.parse_args(["gerrit", "--tools", "a"]).url, proxy_mod.DEFAULT_URL
        )

    def test_server_name_is_required(self):
        with self.assertRaises(SystemExit):
            proxy_mod.parse_args(["--tools", "a"])


if __name__ == "__main__":
    unittest.main()
