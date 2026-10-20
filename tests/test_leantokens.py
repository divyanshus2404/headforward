import http.client
import http.server
import io
import json
import os
import random
import re
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


class AuditTuneTests(unittest.TestCase):
    def make_project(self, d):
        with open(os.path.join(d, "package-lock.json"), "w") as f:
            f.write("{}")
        os.makedirs(os.path.join(d, "dist"))
        with open(os.path.join(d, "CLAUDE.md"), "w") as f:
            f.write("Always follow these detailed rules carefully. " * 600)
        os.makedirs(os.path.join(d, ".claude"))
        with open(os.path.join(d, ".claude", "settings.local.json"), "w") as f:
            json.dump({"model": "opus", "permissions": {"allow": ["Bash(npm test)"], "deny": ["Read(./.env)"]}}, f)

    def test_audit_finds_waste(self):
        with tempfile.TemporaryDirectory() as d:
            self.make_project(d)
            titles = " | ".join(t for _, t, _, _ in lt.find_waste(d))
            self.assertIn("CLAUDE.md is", titles)
            self.assertIn("Lockfiles Claude can read: package-lock.json", titles)
            self.assertIn("Build output Claude can read: dist/", titles)
            sev = dict((t.split(" is ")[0][-9:], s) for s, t, _, _ in lt.find_waste(d) if "CLAUDE.md" in t)
            self.assertIn("high", sev.values())

    def test_tune_preview_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.make_project(d)
            before = open(os.path.join(d, ".claude", "settings.local.json")).read()
            with redirect_stdout(io.StringIO()):
                lt.cmd_tune(["claude", d])
            self.assertEqual(open(os.path.join(d, ".claude", "settings.local.json")).read(), before)

    def test_tune_apply_merges_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            self.make_project(d)
            with redirect_stdout(io.StringIO()):
                lt.cmd_tune(["claude", d, "--apply"])
            data = json.load(open(os.path.join(d, ".claude", "settings.local.json")))
            self.assertEqual(data["model"], "opus")
            self.assertEqual(data["permissions"]["allow"], ["Bash(npm test)"])
            deny = data["permissions"]["deny"]
            self.assertIn("Read(./.env)", deny)
            self.assertIn("Read(./package-lock.json)", deny)
            self.assertIn("Read(./dist/**)", deny)
            self.assertEqual(data["bashOutputMaxChars"], 15000)
            self.assertNotIn("env", data)
            self.assertTrue(os.path.exists(os.path.join(d, ".claude", "settings.local.json.bak")))
            buf = io.StringIO()
            with redirect_stdout(buf):
                lt.cmd_tune(["claude", d, "--apply"])
            self.assertIn("Nothing to change", buf.getvalue())
            self.assertEqual(json.load(open(os.path.join(d, ".claude", "settings.local.json")))["permissions"]["deny"], deny)
            self.assertNotIn("lockfiles Claude can read", " ".join(t.lower() for _, t, _, _ in lt.find_waste(d)))

    def test_tune_refuses_broken_json(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude"))
            with open(os.path.join(d, ".claude", "settings.local.json"), "w") as f:
                f.write("{not json")
            err = io.StringIO()
            from contextlib import redirect_stderr
            with redirect_stderr(err):
                self.assertEqual(lt.cmd_tune(["claude", d, "--apply"]), 1)
            self.assertIn("not valid JSON", err.getvalue())
            self.assertEqual(open(os.path.join(d, ".claude", "settings.local.json")).read(), "{not json")


class UsageMapTests(unittest.TestCase):
    def test_usage_counts_each_reply_once(self):
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "-Users-me-app")
            os.makedirs(proj)
            u = {"input_tokens": 3, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 900}
            lines = [
                {"type": "assistant", "timestamp": "2026-10-19T10:00:00Z", "requestId": "r1",
                 "message": {"id": "m1", "model": "claude-x", "usage": dict(u, output_tokens=5)}},
                {"type": "assistant", "timestamp": "2026-10-19T10:00:01Z", "requestId": "r1",
                 "message": {"id": "m1", "model": "claude-x", "usage": dict(u, output_tokens=40)}},
                {"type": "assistant", "timestamp": "2026-10-20T09:00:00Z", "requestId": "r2",
                 "message": {"id": "m2", "model": "claude-y", "usage": dict(u, output_tokens=7)}},
                {"type": "user", "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "timestamp": "2026-10-20T09:00:00Z", "requestId": "r3",
                 "message": {"id": "m3", "model": "<synthetic>", "usage": dict(u, output_tokens=1)}},
            ]
            with open(os.path.join(proj, "s.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
            rows = lt.read_claude_usage(d)
            self.assertEqual(len(rows), 2)
            self.assertEqual(sorted(r["output"] for r in rows), [7, 40])
            self.assertEqual(sum(r["cache_read"] for r in rows), 1800)
            self.assertEqual(len(lt.read_claude_usage(d, since="2026-10-20")), 1)

    def test_outline_python_and_ts(self):
        py = "import os\n\nclass Cart:\n    def add(self, item):\n        pass\n\nasync def fetch(url):\n    x = 1\n"
        self.assertEqual(lt.outline("a.py", py),
                         ["    3  class Cart", "    4      def add(self, item)", "    7  async def fetch(url)"])
        ts = "export async function load(id: string) {\n}\nexport const save = async (x) => x\nconst y = 2\n"
        self.assertEqual(lt.outline("a.ts", ts),
                         ["    1  export async function load(id: string)", "    3  export const save = async (x) => x"])
        self.assertIsNone(lt.outline("notes.md", "# hi"))

    def test_map_and_pack_outline(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "svc.py"), "w") as f:
                f.write("def handler(event):\n" + "    x = 1\n" * 200 + "\nclass Store:\n    pass\n")
            m = lt.code_map([d])
            self.assertIn("svc.py", m)
            self.assertIn("1  def handler(event)", m)
            full, _, _ = lt.pack_files([d])
            small, _, _ = lt.pack_files([d], outline_code=True)
            self.assertIn("outline only: 2 definitions", small)
            self.assertLess(len(small), len(full) / 5)


class DesktopTests(unittest.TestCase):
    def test_health_endpoint_is_local(self):
        px = lt.start_proxy(upstream="http://127.0.0.1:9")  # nothing listens on port 9
        try:
            c = http.client.HTTPConnection("127.0.0.1", px.server_address[1])
            c.request("GET", "/leantokens/health")
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertTrue(json.loads(r.read())["ok"])
        finally:
            px.shutdown()

    def test_settings_on_off_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".claude", "settings.local.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                json.dump({"model": "opus", "env": {"BASH_MAX_OUTPUT_LENGTH": "30000"}}, f)
            lt._set_base_url(path, lt.DESKTOP_URL)
            on = json.load(open(path))
            self.assertEqual(on["env"]["ANTHROPIC_BASE_URL"], lt.DESKTOP_URL)
            self.assertEqual(on["model"], "opus")
            self.assertTrue(lt._set_base_url(path, None))
            self.assertEqual(json.load(open(path)), {"model": "opus", "env": {"BASH_MAX_OUTPUT_LENGTH": "30000"}})

    @unittest.skipIf(os.name == "nt", "launchd and os.getuid are macOS-only")
    def test_on_chains_in_front_of_existing_gateway_and_off_restores_it(self):
        gateway = "http://127.0.0.1:17322"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".claude", "settings.local.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                json.dump({"env": {"ANTHROPIC_BASE_URL": gateway, "X": "1"}}, f)
            saved = (lt.DESKTOP_PLIST, lt.DESKTOP_STATE, lt._launchctl, lt._healthy, sys.platform)
            lt.DESKTOP_PLIST = os.path.join(d, "agent.plist")
            lt.DESKTOP_STATE = os.path.join(d, "desktop.json")
            lt._launchctl = lambda *a: lt.subprocess.CompletedProcess(a, 0, "", "")
            lt._healthy = lambda timeout=1.0: True
            sys.platform = "darwin"
            try:
                self.assertEqual(lt.desktop_on(d, []), path)
                self.assertEqual(json.load(open(path))["env"]["ANTHROPIC_BASE_URL"], lt.DESKTOP_URL)
                plist = open(lt.DESKTOP_PLIST).read()
                self.assertIn("<string>--upstream</string>\n    <string>%s</string>" % gateway, plist)
                lt.desktop_on(d, [])  # running it again keeps the chain
                self.assertIn(gateway, open(lt.DESKTOP_PLIST).read())
                self.assertEqual(lt.desktop_off(), [path])
                self.assertEqual(json.load(open(path)), {"env": {"ANTHROPIC_BASE_URL": gateway, "X": "1"}})
            finally:
                lt.DESKTOP_PLIST, lt.DESKTOP_STATE, lt._launchctl, lt._healthy, sys.platform = saved

    def test_never_overwrites_another_base_url(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                json.dump({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example.com"}}, f)
            with self.assertRaises(RuntimeError):
                lt._set_base_url(path, lt.DESKTOP_URL)
            self.assertFalse(lt._set_base_url(path, None))  # off leaves someone else's URL alone
            self.assertEqual(json.load(open(path))["env"]["ANTHROPIC_BASE_URL"], "https://gateway.example.com")

    def test_plist_runs_this_file_quietly(self):
        x = lt._plist(["--smart"])
        self.assertIn("<string>proxy</string>", x)
        self.assertIn("<string>--quiet</string>", x)
        self.assertIn("<string>--smart</string>", x)
        self.assertIn("<key>KeepAlive</key><true/>", x)


class CompactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_blob, lt.BLOB_DIR = lt.BLOB_DIR, self.tmp.name

    def tearDown(self):
        lt.BLOB_DIR = self.old_blob
        self.tmp.cleanup()

    def check(self, text, facts, gone=()):
        out = lt.filter_output(text)
        self.assertLess(len(out), len(text))
        for f in facts:
            self.assertIn(f, out)
        for g in gone:
            self.assertNotIn(g, out)
        blob = re.search(r"leantokens show (\w+)", out).group(1)
        with open(os.path.join(lt.BLOB_DIR, blob + ".txt")) as f:
            self.assertEqual(f.read(), text)  # the full output is recoverable
        return out

    def test_git_log(self):
        text = "\n".join(
            "commit %s\nAuthor: Ana Ruiz <ana@example.com>\nDate:   Mon Oct 20 15:4%d:21 2026 +0530\n\n"
            "    Fix refund rounding in order %d\n\n app/orders.py | 1%d ++++++++------\n 1 file changed\n"
            % (("%x" % (0xabc0 + i)) * 10, i, i, i) for i in range(5))
        out = self.check(text, ["author of every commit: Ana Ruiz <ana@example.com>", "2026-10-20 15:43",
                                "Fix refund rounding in order 3", "app/orders.py | 13", "1 file changed"],
                         gone=["++++", "Author:"])
        self.assertEqual(out.count("commit "), 5)

    def test_ls_long(self):
        text = "total 96\n" + "\n".join("-rw-r--r--@  1 ana  staff  %5d Oct 19 21:0%d file_%02d.py" % (100 * i, i % 10, i)
                                         for i in range(12)) + "\ndrwxr-xr-x  4 ana  staff   128 Oct 19 21:01 src"
        self.check(text, ["owner of every entry: ana:staff", "file_11.py", "1100 Oct 19 21:01", "src/"],
                   gone=["total 96", "ana  staff"])

    def test_grep_groups_by_file(self):
        text = "\n".join("src/%s.py:%d:        if order.total < 0:  # TODO refund" % (f, n)
                         for f in ("orders", "refunds") for n in range(10, 70, 6))
        out = self.check(text, ["src/orders.py\n  10:if order.total", "src/refunds.py", "64:if order.total"])
        self.assertEqual(out.count("src/orders.py"), 1)

    def test_timestamped_log_keeps_every_distinct_line(self):
        lines = ["2026-10-20 17:17:%02d.%03d Df bluetoothd[396:1e%03x] Scan matched device %d" % (i % 60, i, i, i)
                 for i in range(40)]
        lines.insert(20, "2026-10-20 17:17:20.500 E  sharingd[77:9] ERROR: AirDrop handshake failed (code -36)")
        out = self.check("\n".join(lines), ["every line is from 2026-10-20", "Scan matched device 0  [x40 similar",
                                             "ERROR: AirDrop handshake failed (code -36)"])
        self.assertNotIn("Scan matched device 7", out)

    def test_other_output_is_left_alone(self):
        text = "\n".join("line %d of a normal program output" % i for i in range(40))
        self.assertEqual(lt._compact_shapes(text), text)


class HookTests(unittest.TestCase):
    def test_rewrites_only_noisy_commands(self):
        for c in ["npm test", "npm install", "pytest -q", "python3 -m unittest discover -v", "cargo build",
                  "CI=1 npm run test:unit", "go test ./...", "pip install -r requirements.txt", "make",
                  "git log --stat", "ls -la", "grep -rn TODO src", "rg TODO", "log show --last 1m",
                  "grep -n 'def ' leantokens.py"]:
            self.assertIsNotNone(lt.rewrite_command(c), c)
        for c in ["ls", "git status", "cat package.json", "git diff", "npm test &", "cd app && npm test",
                  "npm test | tail -5", "python3 script.py", "pytest <<EOF\nx\nEOF", "echo npm test"]:
            self.assertIsNone(lt.rewrite_command(c), c)

    def test_hook_output_shape(self):
        import subprocess as sp
        ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "npm test", "description": "d", "timeout": 5}})
        out = sp.run([sys.executable, lt.__file__, "hook-bash"], input=ev, capture_output=True, text=True).stdout
        h = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        self.assertEqual(h["updatedInput"]["description"], "d")
        self.assertEqual(h["updatedInput"]["timeout"], 5)
        self.assertIn("filter )", h["updatedInput"]["command"])
        self.assertNotIn("permissionDecision", h)
        quiet = sp.run([sys.executable, lt.__file__, "hook-bash"], input='{"tool_input":{"command":"ls"}}',
                       capture_output=True, text=True)
        self.assertEqual((quiet.stdout, quiet.returncode), ("", 0))
        with tempfile.TemporaryDirectory() as home:  # keep the expected error out of the real ~/.leantokens log
            broken = sp.run([sys.executable, lt.__file__, "hook-bash"], input="not json", capture_output=True,
                            text=True, env=dict(os.environ, HOME=home))
        self.assertEqual((broken.stdout, broken.returncode), ("", 0))

    @unittest.skipIf(os.name == "nt", "POSIX shell")
    def test_wrapped_command_keeps_exit_code_and_errors(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            lt.BLOB_DIR = d
            script = os.path.join(d, "fake_tests.py")
            with open(script, "w") as f:
                f.write("import sys\nfor i in range(40): print('test_%d ... ok' % i)\n"
                        "print('FAIL: test_login', file=sys.stderr)\nsys.exit(3)\n")
            cmd = lt.rewrite_command("make") .replace("make", "%s %s" % (sys.executable, script), 1)
            r = sp.run(["bash", "-c", cmd], capture_output=True, text=True, env=dict(os.environ, HOME=d))
            self.assertEqual(r.returncode, 3)
            self.assertIn("FAIL: test_login", r.stdout)
            self.assertIn("passing test lines omitted", r.stdout)
            self.assertLess(len(r.stdout), 300)

    def test_filter_keeps_short_repeats_and_redraws(self):
        self.assertEqual(lt.filter_output("a\na\nb"), "a\na\nb")
        self.assertEqual(lt.filter_output("x\nx\nx\nx"), "x  [x4]")
        self.assertEqual(lt.filter_output("10%\r50%\r100% done"), "100% done")

    def test_install_uninstall_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".claude", "settings.local.json")
            os.makedirs(os.path.dirname(path))
            other = {"matcher": "Write", "hooks": [{"type": "command", "command": "prettier --write"}]}
            with open(path, "w") as f:
                json.dump({"model": "opus", "hooks": {"PreToolUse": [other]}}, f)
            self.assertTrue(lt.hooks_install(d)[1])
            self.assertFalse(lt.hooks_install(d)[1])  # idempotent
            data = json.load(open(path))
            self.assertEqual(len(data["hooks"]["PreToolUse"]), 2)
            self.assertTrue(lt.hooks_uninstall(d)[1])
            self.assertEqual(json.load(open(path)), {"model": "opus", "hooks": {"PreToolUse": [other]}})
            self.assertFalse(lt.hooks_uninstall(d)[1])


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

    def test_settings_base_url_project_beats_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".claude"))
            self.assertIsNone(lt._get_base_url(os.path.join(tmp, ".claude", "settings.local.json")))
            with open(os.path.join(tmp, ".claude", "settings.local.json"), "w") as f:
                json.dump({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:17322"}}, f)
            self.assertEqual(lt._claude_settings_base_url(tmp), "http://127.0.0.1:17322")

    def test_missing_agent(self):
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err):
            self.assertEqual(lt.cmd_wrap(["no-such-agent-xyz"]), 127)
        self.assertIn("not installed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
