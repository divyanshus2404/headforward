import http.client
import http.server
import io
import json
import os
import random
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import leantokens as lt  # noqa: E402

ROWS = [{"id": i, "status": "shipped", "customer": "cust-%d" % i, "total": 19.99} for i in range(40)]
LOGS = "\n".join(["\x1b[32mINFO\x1b[0m GET /health 200"] * 30 + ["ERROR db timeout"])
SIMILAR = "\n".join("10:%02d:%02d worker-%d processed job %d in %dms" % (i // 60, i % 60, i % 4, 1000 + i, 20 + i)
                    for i in range(40)) + "\nERROR job 1041 failed: disk full"


def random_json(rng, depth=0):
    kind = rng.choice(["obj", "list", "table", "scalar"] if depth < 3 else ["scalar"])
    if kind == "obj":
        return {"k%d" % i: random_json(rng, depth + 1) for i in range(rng.randint(0, 4))}
    if kind == "list":
        return [random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    if kind == "table":
        keys = ["a", "b", "c", "d"][: rng.randint(1, 4)]
        const = rng.choice(keys)
        return [{k: ("same" if k == const else random_json(rng, depth + 1)) for k in keys}
                for _ in range(rng.randint(3, 6))]
    return rng.choice([None, True, False, 0, -3, 2.5, "", "text", "ünïcode"])


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
        self.assertEqual(lt.compress('```json\n{\n  "a": 1\n}\n```', "high"), '```json\n{"a":1}\n```')


class ToolTests(unittest.TestCase):
    def test_json_table_hoists_constant_columns(self):
        out = json.loads(lt.compress_tool(json.dumps(ROWS, indent=2)))
        self.assertEqual(out["_cols"], ["id", "customer"])
        self.assertEqual(out["_same"], {"status": "shipped", "total": 19.99})
        self.assertEqual(out["_rows"][3], [3, "cust-3"])

    def test_json_round_trip_is_lossless(self):
        rng = random.Random(7)
        for _ in range(500):
            data = random_json(rng)
            text = json.dumps(data, indent=2) + " " * 200
            out = lt.compress_tool(text)
            # Compare as JSON text so 0 vs False and 1 vs 1.0 count as different.
            self.assertEqual(json.dumps(lt.expand(json.loads(out)), sort_keys=True),
                             json.dumps(data, sort_keys=True))

    def test_bool_and_zero_columns_not_merged(self):
        data = [{"id": i, "flag": [0, False, 0][i % 3]} for i in range(6)]
        out = lt.compress_tool(json.dumps(data, indent=2) + " " * 200)
        self.assertEqual(json.dumps(lt.expand(json.loads(out))), json.dumps(data))

    def test_reserved_keys_are_not_tabulated(self):
        data = [{"_cols": i, "x": 1} for i in range(10)]
        out = json.loads(lt.compress_tool(json.dumps(data, indent=2)))
        self.assertEqual(out, data)

    def test_rows_with_different_key_order_still_tabulate(self):
        data = [{"a": i, "b": 2} if i % 2 else {"b": 2, "a": i} for i in range(10)]
        out = json.loads(lt.compress_tool(json.dumps(data, indent=2)))
        self.assertIn("_cols", out)
        self.assertEqual(lt.expand(out), data)

    def test_logs_collapsed(self):
        out = lt.compress_tool(LOGS)
        self.assertIn("INFO GET /health 200  [x30]", out)
        self.assertNotIn("\x1b", out)

    def test_similar_lines_only_collapse_when_lossy(self):
        self.assertNotIn("similar lines omitted", lt.compress_tool(SIMILAR))
        out = lt.compress_tool(SIMILAR, lossy=True)
        self.assertIn("[38 similar lines omitted]", out)
        self.assertIn("processed job 1000", out)
        self.assertIn("processed job 1039", out)
        self.assertIn("ERROR job 1041 failed: disk full", out)

    def test_short_text_passthrough(self):
        self.assertEqual(lt.compress_tool("short"), "short")
        self.assertEqual(lt.compress_tool("x" * 300), "x" * 300)


class DedupeTests(unittest.TestCase):
    def body(self, n):
        return {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t%d" % i, "content": LOGS}]} for i in range(n)]}

    def test_repeated_result_points_to_first(self):
        b = self.body(3)
        lt.compress_request(b)
        self.assertIn("[x30]", b["messages"][0]["content"][0]["content"])
        self.assertEqual(b["messages"][1]["content"][0]["content"], "[Same output as tool call t0 above.]")
        self.assertEqual(b["messages"][2]["content"][0]["content"], "[Same output as tool call t0 above.]")

    def test_earlier_turns_unchanged_as_conversation_grows(self):
        short, longer = self.body(2), self.body(4)
        lt.compress_request(short)
        lt.compress_request(longer)
        self.assertEqual(short["messages"], longer["messages"][:2])


class UsageTests(unittest.TestCase):
    def test_anthropic_stream(self):
        raw = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
               b'{"input_tokens":100,"cache_read_input_tokens":900,"cache_creation_input_tokens":50,'
               b'"output_tokens":1}}}\n\nevent: message_delta\ndata: {"type":"message_delta",'
               b'"usage":{"output_tokens":42}}\n\n')
        self.assertEqual(lt.parse_usage(raw, "anthropic"), {"in": 1050, "cached": 900, "out": 42})

    def test_openai_responses(self):
        raw = json.dumps({"usage": {"input_tokens": 500, "output_tokens": 20,
                                    "input_tokens_details": {"cached_tokens": 300}}}).encode()
        self.assertEqual(lt.parse_usage(raw, "openai"), {"in": 500, "cached": 300, "out": 20})

    def test_openai_chat(self):
        raw = json.dumps({"usage": {"prompt_tokens": 70, "completion_tokens": 5}}).encode()
        self.assertEqual(lt.parse_usage(raw, "openai"), {"in": 70, "cached": 0, "out": 5})

    def test_summary_uses_billed_rate(self):
        s = lt.summarize([{"t": 0, "p": "anthropic", "b": 4000, "a": 1000, "req": 6000,
                           "in": 1500, "cached": 1000, "out": 10}])
        self.assertEqual(s["saved"], 750)  # 3000 chars removed at 0.25 tokens/char


class FakeAPI(http.server.BaseHTTPRequestHandler):
    got = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.got.update(body=body, key=self.headers.get("x-api-key"), path=self.path)
        if self.path.endswith("/count_tokens"):
            out = json.dumps({"input_tokens": len(json.dumps(body)) // 4}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if body.get("stream") is False:
            out = json.dumps({"content": [{"type": "tool_use", "name": "fix", "input": {"file": "a.py"}}],
                              "usage": {"input_tokens": 10, "output_tokens": 3}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'data: {"type":"message_start","message":{"usage":{"input_tokens":300,'
                         b'"cache_read_input_tokens":700,"output_tokens":1}}}\n\n')
        for i in range(3):
            self.wfile.write(b"data: chunk%d\n\n" % i)
            self.wfile.flush()
        self.wfile.write(b'data: {"type":"message_delta","usage":{"output_tokens":25}}\n\n')


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.rec = os.path.join(cls.tmp.name, "rec")
        lt.STATS_FILE = os.path.join(cls.tmp.name, "stats.jsonl")
        cls.upstream = "http://127.0.0.1:%d" % cls.up.server_address[1]
        cls.rows = []
        cls.px = lt.start_proxy(upstream=cls.upstream, log=cls.rows.append, record=cls.rec)

    @classmethod
    def tearDownClass(cls):
        cls.px.shutdown()
        cls.up.shutdown()
        cls.tmp.cleanup()

    def post(self, body):
        c = http.client.HTTPConnection("127.0.0.1", self.px.server_address[1])
        c.request("POST", "/v1/messages", body, {"Content-Type": "application/json",
                                                 "x-api-key": "secret-test-key", "anthropic-version": "2023-06-01"})
        r = c.getresponse()
        out = r.status, r.read().decode()
        c.close()
        return out

    def request(self):
        return {"model": "m", "max_tokens": 100, "system": "be terse", "messages": [
            {"role": "user", "content": "check orders"},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(ROWS, indent=2)},
                {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": LOGS}]},
            ]},
        ]}

    def test_anthropic_request(self):
        raw = json.dumps(self.request())
        status, stream = self.post(raw)
        g = FakeAPI.got
        b = g["body"]
        self.assertEqual(status, 200)
        self.assertIn("data: chunk0\n\ndata: chunk1\n\ndata: chunk2\n\n", stream)
        self.assertEqual(g["key"], "secret-test-key")
        self.assertEqual(b["system"], "be terse")
        self.assertEqual(b["messages"][0]["content"], "check orders")
        self.assertIn("_cols", b["messages"][1]["content"][0]["content"])
        first = b
        self.post(raw)
        self.assertEqual(FakeAPI.got["body"], first, "compression must be deterministic")

    def test_usage_recorded_and_no_secrets_saved(self):
        self.post(json.dumps(self.request()))
        row = self.rows[-1]
        self.assertEqual((row["in"], row["cached"], row["out"]), (1000, 700, 25))
        texts = [open(os.path.join(self.rec, f)).read() for f in os.listdir(self.rec)]
        self.assertTrue(texts)
        self.assertFalse(any("secret-test-key" in t for t in texts))
        self.assertTrue(any('"tool_use_id": "t1"' in t for t in texts))

    def test_openai_tool_messages(self):
        req = {"messages": [{"role": "tool", "tool_call_id": "c1", "content": LOGS}],
               "input": [{"type": "function_call_output", "call_id": "c2", "output": LOGS}]}
        self.post(json.dumps(req))
        b = FakeAPI.got["body"]
        self.assertIn("[x30]", b["messages"][0]["content"])
        self.assertEqual(b["input"][0]["output"], "[Same output as tool call c1 above.]")

    def test_eval_replays_recordings(self):
        self.post(json.dumps(self.request()))
        old = dict(lt.UPSTREAMS)
        lt.UPSTREAMS["anthropic"] = self.upstream
        os.environ["ANTHROPIC_API_KEY"] = "test"
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                lt.cmd_eval([self.rec])
        finally:
            lt.UPSTREAMS.update(old)
            del os.environ["ANTHROPIC_API_KEY"]
        out = buf.getvalue()
        self.assertIn("Total input:", out)
        self.assertRegex(out, r"Answers: \d+ same \(of \d+\)")


if __name__ == "__main__":
    unittest.main()
