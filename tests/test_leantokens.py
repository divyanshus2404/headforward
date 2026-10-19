import http.client
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import leantokens as lt  # noqa: E402

ROWS = [{"id": i, "status": "shipped", "customer": "cust-%d" % i, "total": 19.99} for i in range(40)]
LOGS = "\n".join(["\x1b[32mINFO\x1b[0m GET /health 200"] * 30 + ["ERROR db timeout"])


class PromptTests(unittest.TestCase):
    def test_medium_cuts_filler_and_repeats(self):
        text = "Hi Claude! Could you please make sure it is able to handle a large number of items? " \
               "Could you please make sure it is able to handle a large number of items? Thanks!"
        self.assertEqual(lt.compress(text, "medium"), "Ensure it can handle many items?")

    def test_code_untouched(self):
        text = "Please fix:\n```python\n# the   comment\ndef f(the_list):\n    return the_list\n```"
        self.assertIn("# the   comment\ndef f(the_list):\n    return the_list", lt.compress(text, "high"))

    def test_inline_code_spacing_kept(self):
        self.assertEqual(lt.compress("Pass `--price` (USD) now", "high"), "Pass `--price` (USD) now")

    def test_json_block_minified_at_high(self):
        out = lt.compress('```json\n{\n  "a": 1\n}\n```', "high")
        self.assertEqual(out, '```json\n{"a":1}\n```')


class ToolTests(unittest.TestCase):
    def test_json_tabulated(self):
        out = json.loads(lt.compress_tool(json.dumps(ROWS, indent=2)))
        self.assertEqual(out["_cols"], ["id", "status", "customer", "total"])
        self.assertEqual(out["_rows"][3], [3, "shipped", "cust-3", 19.99])

    def test_logs_collapsed(self):
        out = lt.compress_tool(LOGS)
        self.assertIn("INFO GET /health 200  [x30]", out)
        self.assertNotIn("\x1b", out)

    def test_short_text_passthrough(self):
        self.assertEqual(lt.compress_tool("short"), "short")
        self.assertEqual(lt.compress_tool("x" * 300), "x" * 300)


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.got = got = {}

        class Fake(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                got["key"] = self.headers.get("x-api-key")
                got["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for i in range(3):
                    self.wfile.write(b"data: chunk%d\n\n" % i)
                    self.wfile.flush()

        cls.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        lt.STATS_FILE = os.path.join(cls.tmp.name, "stats.jsonl")
        cls.px = lt.start_proxy(upstream="http://127.0.0.1:%d" % cls.up.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.px.shutdown()
        cls.up.shutdown()
        cls.tmp.cleanup()

    def post(self, body):
        c = http.client.HTTPConnection("127.0.0.1", self.px.server_address[1])
        c.request("POST", "/v1/messages", body, {"Content-Type": "application/json",
                                                 "x-api-key": "test-key", "anthropic-version": "2023-06-01"})
        r = c.getresponse()
        return r.status, r.read().decode()

    def test_anthropic_request(self):
        req = {"system": "be terse", "messages": [
            {"role": "user", "content": "check orders"},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(ROWS, indent=2)},
                {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": LOGS}]},
            ]},
        ]}
        raw = json.dumps(req)
        status, stream = self.post(raw)
        b = self.got["body"]
        self.assertEqual(status, 200)
        self.assertEqual(stream, "data: chunk0\n\ndata: chunk1\n\ndata: chunk2\n\n")
        self.assertEqual(self.got["path"], "/v1/messages")
        self.assertEqual(self.got["key"], "test-key")
        self.assertEqual(b["system"], "be terse")
        self.assertEqual(b["messages"][0]["content"], "check orders")
        self.assertIn("_cols", b["messages"][1]["content"][0]["content"])
        self.assertIn("[x30]", b["messages"][1]["content"][1]["content"][0]["text"])
        first = b
        self.post(raw)
        self.assertEqual(self.got["body"], first, "compression must be deterministic")

    def test_openai_tool_messages(self):
        req = {"messages": [{"role": "tool", "tool_call_id": "c1", "content": LOGS}],
               "input": [{"type": "function_call_output", "call_id": "c2", "output": LOGS}]}
        self.post(json.dumps(req))
        b = self.got["body"]
        self.assertIn("[x30]", b["messages"][0]["content"])
        self.assertIn("[x30]", b["input"][0]["output"])


if __name__ == "__main__":
    unittest.main()
