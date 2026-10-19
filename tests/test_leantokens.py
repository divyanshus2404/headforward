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


FILE = "\n".join("%d:    line %d of the module" % (n, n) for n in range(1, 60))


class SmartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        lt.BLOB_DIR = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def body(self, *outputs):
        return {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t%d" % i, "content": o}]} for i, o in enumerate(outputs)]}

    def test_file_view_starting_with_bracket_is_referenced(self):
        first = "[File: app.py (59 lines total)]\n" + FILE
        edited = "[File: app.py (59 lines total)]\n" + FILE.replace("line 30 of", "EDITED line 30 of")
        b = self.body(first, edited)
        lt.compress_request(b, smart=True)
        out = b["messages"][1]["content"][0]["content"]
        self.assertIn("EDITED line 30", out)
        # Header + module lines 1-29 match t0's lines 1-30; lines 31-59 match t0's lines 32-60.
        self.assertIn("[30 lines identical to lines 1-30 of tool call t0]", out)
        self.assertIn("[29 lines identical to lines 32-60 of tool call t0]", out)
        self.assertEqual(b["messages"][0]["content"][0]["content"], first)

    def test_renumbered_lines_after_insert_still_match(self):
        code = ["def handler_%d(event):" % i if i % 5 == 0 else "    value_%d = compute(event, %d)" % (i, i)
                for i in range(1, 41)]
        view = lambda ls: "[File: svc.py]\n" + "\n".join("%d:%s" % (n, l) for n, l in enumerate(ls, 1))
        edited = code[:10] + ["    log(event)", "    audit(event)"] + code[10:]
        b = self.body(view(code), view(edited))
        lt.compress_request(b, smart=True)
        out = b["messages"][1]["content"][0]["content"]
        self.assertIn("11:    log(event)", out)
        self.assertIn("[30 lines: same text as lines 12-41 of tool call t0, numbered 13-42 here]", out)

    def test_references_off_by_default(self):
        b = self.body("x\n" + FILE, "y\n" + FILE)
        lt.compress_request(b)
        self.assertNotIn("identical to lines", b["messages"][1]["content"][0]["content"])

    def test_reference_points_at_the_right_lines(self):
        a = "header\n" + FILE
        b = self.body(a, "other\n" + "\n".join(FILE.split("\n")[9:20]))
        lt.compress_request(b, smart=True)
        ref = b["messages"][1]["content"][0]["content"].split("\n")[1]
        self.assertEqual(ref, "[11 lines identical to lines 11-21 of tool call t0]")
        self.assertEqual(a.split("\n")[10:21], FILE.split("\n")[9:20])

    def test_huge_output_is_expandable(self):
        big = "\n".join("step %d ok" % i for i in range(1000)).replace("step 500 ok", "step 500 FAILED: boom")
        b = self.body(big)
        lt.compress_request(b, smart=True)
        out = b["messages"][0]["content"][0]["content"]
        self.assertLess(len(out), len(big) / 3)
        self.assertIn("501: step 500 FAILED: boom", out)
        blob = lt.re.search(r"leantokens show ([0-9a-f]{12})", out).group(1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            lt.cmd_show([blob, "--lines", "500-502"])
        self.assertEqual(buf.getvalue(), "500: step 499 ok\n501: step 500 FAILED: boom\n502: step 501 ok\n")

    def test_smart_is_deterministic_and_keeps_earlier_turns(self):
        outs = ["[File: a]\n" + FILE, "[File: a]\n" + FILE.replace("line 5 ", "line five "), "[File: a]\n" + FILE]
        short, longer = self.body(*outs[:2]), self.body(*outs)
        lt.compress_request(short, smart=True)
        lt.compress_request(longer, smart=True)
        self.assertEqual(short["messages"], longer["messages"][:2])


class NoiseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        lt.BLOB_DIR = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_pytest_keeps_failures_and_summary(self):
        text = "\n".join(["tests/test_api.py::test_%d PASSED" % i for i in range(30)] +
                         ["tests/test_api.py::test_login FAILED", "E   assert 401 == 200",
                          "=== 1 failed, 30 passed in 2.10s ==="])
        out = lt._filter_noise(text)
        self.assertIn("[30 passing test lines omitted; full output: leantokens show ", out)
        self.assertIn("test_login FAILED", out)
        self.assertIn("E   assert 401 == 200", out)
        self.assertIn("1 failed, 30 passed", out)

    def test_pip_noise_collapses_but_errors_stay(self):
        text = "\n".join(["Collecting pkg%d" % i for i in range(10)] +
                         ["ERROR: Could not find a version that satisfies the requirement nopkg"])
        out = lt._filter_noise(text)
        self.assertIn("[10 package install lines omitted", out)
        self.assertIn("ERROR: Could not find a version", out)

    def test_full_output_recoverable(self):
        text = "\n".join("Compiling crate%d v0.%d.0" % (i, i) for i in range(12)) + "\nerror[E0425]: cannot find value"
        out = lt._filter_noise(text)
        blob = lt.re.search(r"show ([0-9a-f]{12})", out).group(1)
        self.assertEqual(open(os.path.join(self.tmp.name, blob + ".txt")).read(), text)

    def test_plain_code_untouched(self):
        self.assertEqual(lt._filter_noise(FILE), FILE)


class ClaudeChatTests(unittest.TestCase):
    def test_compress_any_picks_rules(self):
        self.assertIn("_cols", lt.compress_any(json.dumps(ROWS, indent=2)))
        self.assertEqual(lt.compress_any("Hi Claude! Could you please check this? Thanks!"), "Check this?")

    def test_pack_skips_secrets_binaries_and_lockfiles(self):
        with tempfile.TemporaryDirectory() as d:
            for name, data in [("app.py", b"print('hi')   \n"), (".env", b"KEY=secret"), ("id_rsa", b"x"),
                               ("package-lock.json", b"{}"), ("logo.png", b"\x89PNG"), ("blob.dat", b"a\0b"),
                               ("data.json", json.dumps(ROWS, indent=2).encode())]:
                with open(os.path.join(d, name), "wb") as f:
                    f.write(data)
            os.makedirs(os.path.join(d, "node_modules", "x"))
            with open(os.path.join(d, "node_modules", "x", "i.js"), "w") as f:
                f.write("junk")
            text, included, skipped = lt.pack_files([d])
            names = sorted(os.path.basename(f) for f, _, _ in included)
            self.assertEqual(names, ["app.py", "data.json"])
            self.assertNotIn("secret", text)
            self.assertIn("print('hi')\n</document>", text)  # trailing spaces trimmed
            self.assertIn("_cols", text)                      # JSON compressed
            why = dict((os.path.basename(f), r) for f, r in skipped)
            self.assertEqual(why[".env"], "may contain secrets")
            self.assertEqual(why["blob.dat"], "binary")

    def test_pack_respects_budget(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(5):
                with open(os.path.join(d, "f%d.txt" % i), "w") as f:
                    f.write("word " * 400)
            text, included, skipped = lt.pack_files([d], budget=1200)
            self.assertLessEqual(lt.count(text)[0], 1200)
            self.assertTrue(skipped)

    def test_count_estimate_without_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("Hi Claude! Could you please check this? Thanks! " * 10)
        buf = io.StringIO()
        with redirect_stdout(buf):
            lt.cmd_count([f.name])
        os.unlink(f.name)
        self.assertIn("estimate", buf.getvalue())
        self.assertIn("fewer", buf.getvalue())


class CacheTests(unittest.TestCase):
    def test_adds_markers(self):
        b = {"system": "sys", "messages": [{"role": "user", "content": "hi"}]}
        self.assertTrue(lt.add_cache_control(b))
        self.assertEqual(b["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(b["messages"][-1]["content"][-1]["cache_control"], {"type": "ephemeral"})

    def test_leaves_existing_markers_alone(self):
        b = {"system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
             "messages": [{"role": "user", "content": "hi"}]}
        self.assertFalse(lt.add_cache_control(b))
        self.assertEqual(b["messages"][0]["content"], "hi")


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

    def test_count_exact_with_key(self):
        old = dict(lt.UPSTREAMS)
        lt.UPSTREAMS["anthropic"] = self.upstream
        os.environ["ANTHROPIC_API_KEY"] = "test"
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                f.write(json.dumps(ROWS, indent=2))
            buf = io.StringIO()
            with redirect_stdout(buf):
                lt.cmd_count([f.name])
            os.unlink(f.name)
        finally:
            lt.UPSTREAMS.update(old)
            del os.environ["ANTHROPIC_API_KEY"]
        self.assertIn("exact, claude-sonnet-5-5", buf.getvalue())
        self.assertEqual(FakeAPI.got["path"], "/v1/messages/count_tokens")

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


FAKE_CLAUDE = r'''#!%s
import http.client, json, os, signal, sys, time, urllib.parse
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
    sys.exit(0)
os.kill(os.getppid(), signal.SIGINT)  # the user presses Ctrl-C to cancel a response
time.sleep(0.2)
u = urllib.parse.urlsplit(os.environ["ANTHROPIC_BASE_URL"])
c = http.client.HTTPConnection(u.hostname, u.port)
logs = "\n".join(["INFO GET /health 200"] * 30)
body = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "a", "content": logs}]}]}
c.request("POST", "/v1/messages", json.dumps(body), {"Content-Type": "application/json",
          "Authorization": "Bearer user-oauth-token", "anthropic-version": "2023-06-01"})
r = c.getresponse()
r.read()
sys.exit(0 if r.status == 200 else 3)
'''


@unittest.skipIf(os.name == "nt", "uses a POSIX stand-in for the claude command")
class WrapTests(unittest.TestCase):
    def test_wrap_end_to_end(self):
        up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp:
            agent = os.path.join(tmp, "claude")
            with open(agent, "w") as f:
                f.write(FAKE_CLAUDE % sys.executable)
            os.chmod(agent, 0o755)
            lt.STATS_FILE = os.path.join(tmp, "stats.jsonl")
            old_path = os.environ["PATH"]
            os.environ["PATH"] = tmp + os.pathsep + old_path
            err = io.StringIO()
            try:
                from contextlib import redirect_stderr
                with redirect_stderr(err):
                    code = \
                        lt.cmd_wrap(["--smart", "claude"], "http://127.0.0.1:%d" % up.server_address[1])
            finally:
                os.environ["PATH"] = old_path
                up.shutdown()
        self.assertEqual(code, 0, err.getvalue())  # survived Ctrl-C, request succeeded
        self.assertEqual(FakeAPI.got["body"]["messages"][0]["content"][0]["content"],
                         "INFO GET /health 200  [x30]")
        self.assertIn("compressing claude (smart)", err.getvalue())
        self.assertIn("this session", err.getvalue())
        self.assertNotIn("not signed in", err.getvalue())

    def test_missing_agent(self):
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err):
            self.assertEqual(lt.cmd_wrap(["no-such-agent-xyz"]), 127)
        self.assertIn("not installed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
