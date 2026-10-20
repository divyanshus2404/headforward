"""Compare leantokens, rtk and Headroom on the same real command outputs.

macOS. Needs tiktoken, rtk on PATH, and Headroom installed (`uv tool install
headroom-ai` or pip). Captures outputs on this machine, so numbers vary:

  python3 bench/compare_tools.py

Writes bench/compare/results.json and prints a table. Token counts use tiktoken cl100k_base as a stand-in for
Claude's tokenizer (same for every tool, so the comparison is fair).
"""
import json, os, re, shutil, subprocess, sys

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compare")
TOKEN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_hr = shutil.which("headroom")
HEADROOM_PY = os.environ.get("HEADROOM_PYTHON") or (open(_hr).readline()[2:].strip() if _hr else sys.executable)
sys.path.insert(0, TOKEN)
import leantokens as lt  # noqa: E402
import tiktoken  # noqa: E402

enc = tiktoken.get_encoding("cl100k_base")
tok = lambda s: len(enc.encode(s, disallowed_special=()))


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=300,
                       env=dict(os.environ, NO_COLOR="", CLICOLOR_FORCE="1"))
    return r.stdout + r.stderr


# ---- fixtures -------------------------------------------------------------
os.makedirs(os.path.join(HERE, "work"), exist_ok=True)
W = os.path.join(HERE, "work")
with open(os.path.join(W, "test_bench.py"), "w") as f:
    f.write("import unittest\n\nclass Orders(unittest.TestCase):\n")
    for i in range(60):
        f.write("    def test_order_%02d(self):\n        self.assertEqual(%d * 2, %d)\n" % (i, i, i * 2))
    f.write("    def test_fail_refund_total(self):\n        self.assertEqual(round(19.99 * 3, 2), 59.98)\n")
    f.write("    def test_fail_missing_sku(self):\n        self.assertIn('SKU-881', {'SKU-880': 1})\n")

site = sh(HEADROOM_PY + " -c 'import site;print(site.getsitepackages()[0])'").strip()

logf = os.path.join(W, "system.log")
lines = sh("log show --last 45s --style compact").splitlines()[:600]
open(logf, "w").write("\n".join(lines) + "\n")
err_line = next((l for l in lines if re.search(r"\berror\b", l, re.I)), "")
log_fact = err_line[-40:].strip() if err_line else ""

brew = json.loads(subprocess.run("brew info --json=v2 --installed", shell=True, capture_output=True,
                                 text=True, timeout=300).stdout or "{}")
formulae = [{"name": f["name"], "desc": f.get("desc"), "license": f.get("license"),
             "version": f["versions"]["stable"], "homepage": f.get("homepage"),
             "installed": [i["version"] for i in f.get("installed", [])],
             "dependencies": f.get("dependencies", []), "outdated": f.get("outdated")}
            for f in brew.get("formulae", [])[:25]]
jsonf = os.path.join(W, "brew.json")
open(jsonf, "w").write(json.dumps(formulae, indent=2))

CASES = [
    dict(name="unit tests (2 failures)", cwd=W,
         raw="python3 -m unittest -v test_bench", rtk="rtk test python3 -m unittest -v test_bench",
         facts=["test_fail_refund_total", "test_fail_missing_sku", "59.98", "SKU-881"]),
    dict(name="git log --stat (40)", cwd=TOKEN,
         raw="git log -n 40 --stat", rtk="rtk git log -n 40 --stat",
         facts=["show realistic savings", "chain in front", "leantokens.py"]),
    dict(name="grep 'def ' (1 file)", cwd=TOKEN,
         raw="grep -n 'def ' leantokens.py", rtk="rtk grep -n 'def ' leantokens.py",
         facts=["def cmd_wrap", "def filter_output", "def desktop_on"]),
    dict(name="grep -rn 'def ' (3 paths)", cwd=TOKEN,
         raw="grep -rn --include='*.py' 'def ' leantokens.py tests bench", rtk="rtk grep -rn --include='*.py' 'def ' leantokens.py tests bench",
         facts=["def test_git_log", "def cmd_wrap", "bench_traces.py"]),
    dict(name="ls -la site-packages", cwd=W,
         raw="ls -la " + site, rtk="rtk ls -la " + site,
         facts=["headroom", "tiktoken" if os.path.exists(os.path.join(site, "tiktoken")) else "headroom"]),
    dict(name="macOS system log (600 lines)", cwd=W,
         raw="cat system.log", rtk="rtk log system.log", hook_cmd="log show --last 45s --style compact",
         facts=[log_fact] if log_fact else []),
    dict(name="JSON (brew info, 25 items)", cwd=W,
         raw="cat brew.json", rtk="rtk json brew.json",
         facts=[formulae[0]["name"], formulae[-1]["name"], formulae[-1]["version"]] if formulae else []),
]

HR_SCRIPT = '"""Run Headroom\'s default compress() on each raw output as a Bash tool result."""\nimport json, sys\nfrom headroom import compress\n\ncases = json.load(open(sys.argv[1]))\nout = []\nfor c in cases:\n    msgs = [{"role": "user", "content": "check this"},\n            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}]},\n            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": c["raw"]}]},\n            {"role": "assistant", "content": "Looking at it."},\n            {"role": "user", "content": "go on"}]\n    r = compress(msgs, model="claude-sonnet-4-5")\n    content = r.messages[2]["content"]\n    block = content[0] if isinstance(content, list) else {"content": content}\n    text = block.get("content")\n    if isinstance(text, list):\n        text = "\\n".join(b.get("text", "") for b in text if isinstance(b, dict))\n    out.append(text)\nprint(json.dumps(out))\n'

# ---- run ------------------------------------------------------------------
def msgs(text):
    return [{"role": "user", "content": "check this"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]},
            {"role": "assistant", "content": "Looking at it."},
            {"role": "user", "content": "go on"}]


for c in CASES:
    raw = sh(c["raw"], c["cwd"])
    c["out"] = {"raw": raw, "rtk": sh(c["rtk"], c["cwd"])}
    rewritten = lt.rewrite_command(c.get("hook_cmd", c["raw"]))
    hook = lt.filter_output(raw) if rewritten else raw
    c["hook_applies"] = bool(rewritten)
    c["out"]["leantokens hook"] = hook
    for label, text, lossy in (("leantokens proxy", raw, False), ("leantokens hook+proxy", hook, False),
                               ("leantokens proxy lossy", raw, True)):
        data = {"model": "claude-sonnet-4-5", "max_tokens": 1024, "messages": msgs(text)}
        lt.compress_request(data, lossy, False)
        out = data["messages"][2]["content"][0]["content"]
        c["out"][label] = out if isinstance(out, str) else "\n".join(b.get("text", "") for b in out)

json.dump([{"name": c["name"], "raw": c["out"]["raw"]} for c in CASES], open(os.path.join(W, "raw.json"), "w"))
hr_script = os.path.join(W, "hr.py")
open(hr_script, "w").write(HR_SCRIPT)
hr = subprocess.run([HEADROOM_PY, hr_script, os.path.join(W, "raw.json")],
                    capture_output=True, text=True, timeout=900)
hr_out = json.loads(hr.stdout.strip().splitlines()[-1]) if hr.returncode == 0 else None
if hr_out is None:
    print("headroom failed:", hr.stderr[-2000:])
for c, h in zip(CASES, hr_out or [None] * len(CASES)):
    c["out"]["headroom"] = h if h is not None else c["out"]["raw"]

TOOLS = ["raw", "rtk", "headroom", "leantokens hook", "leantokens proxy", "leantokens hook+proxy", "leantokens proxy lossy"]
rows = []
for c in CASES:
    r = {"case": c["name"], "hook_applies": c["hook_applies"]}
    for t in TOOLS:
        text = c["out"][t]
        r[t] = {"tokens": tok(text),
                "facts_kept": sum(1 for f in c["facts"] if f and f in text), "facts": len(c["facts"])}
        open(os.path.join(W, "%s__%s.txt" % (re.sub(r"\W+", "_", c["name"]), t.replace(" ", "_").replace("+", "_"))), "w").write(text)
    rows.append(r)
json.dump(rows, open(os.path.join(HERE, "results.json"), "w"), indent=1)

w = 28
print("%-*s" % (w, "case") + "".join("%17s" % t.replace("leantokens", "lt").replace("proxy", "px") for t in TOOLS))
tot = {t: 0 for t in TOOLS}
for r in rows:
    cells = []
    for t in TOOLS:
        x = r[t]
        tot[t] += x["tokens"]
        mark = "" if x["facts"] == 0 else (" ✓" if x["facts_kept"] == x["facts"] else " ✗%d/%d" % (x["facts_kept"], x["facts"]))
        cells.append("%17s" % ("%d%s" % (x["tokens"], mark)))
    print("%-*s" % (w, r["case"][:w]) + "".join(cells))
print("%-*s" % (w, "TOTAL") + "".join("%17d" % tot[t] for t in TOOLS))
print("%-*s" % (w, "saved vs raw") + "".join("%16.0f%%" % (100 * (1 - tot[t] / tot["raw"])) for t in TOOLS))
