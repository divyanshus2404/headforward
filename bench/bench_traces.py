"""Benchmark headforward on real coding-agent sessions.

Uses public SWE-agent trajectories (nebius/SWE-agent-trajectories on Hugging Face):
real sessions where an agent fixes GitHub issues by reading files, searching,
editing and running tests. Every tool output is replayed as a tool_result.

  python3 bench/bench_traces.py --sessions 200

Reports two numbers per mode:
  final context   tokens in the last request of each session
  session input   tokens sent across every request in the session, which is
                  what an agent without prompt caching pays for
Needs `pip install tiktoken`.
"""
import argparse
import copy
import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import headforward as lt  # noqa: E402

API = ("https://datasets-server.huggingface.co/rows?dataset=nebius/SWE-agent-trajectories"
       "&config=default&split=train&offset=%d&length=%d")
CACHE = os.path.join(os.path.dirname(__file__), "data")


def fetch(n, seed_offset):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "trajectories-%d-%d.json" % (seed_offset, n))
    if os.path.exists(path):
        return json.load(open(path))
    rows = []
    for off in range(seed_offset, seed_offset + n, 100):
        url = API % (off, min(100, seed_offset + n - off))
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    rows += [x["row"]["trajectory"] for x in json.load(r)["rows"]]
                break
            except OSError:
                time.sleep(2 ** attempt)
    json.dump(rows, open(path, "w"))
    return rows


def to_requests(traj):
    """Rebuild the sequence of API requests the agent sent, as Anthropic bodies."""
    msgs, reqs, n = [], [], 0
    turns = [t for t in traj if t.get("role") in ("user", "ai")]
    for i, t in enumerate(turns):
        text = t.get("text") or ""
        if t["role"] == "ai":
            reqs.append({"messages": copy.deepcopy(msgs)})
            msgs.append({"role": "assistant", "content": [
                {"type": "text", "text": text},
                {"type": "tool_use", "id": "call_%d" % n, "name": "bash", "input": {}}]})
        elif i == 0:
            msgs.append({"role": "user", "content": text})  # the task
        else:
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_%d" % n, "content": text}]})
            n += 1
    return reqs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=200)
    ap.add_argument("--offset", type=int, default=0)
    a = ap.parse_args()
    import tiktoken
    enc = tiktoken.get_encoding("o200k_base")
    tok = lambda b: len(enc.encode(json.dumps(b, ensure_ascii=False), disallowed_special=()))

    sessions = [s for s in fetch(a.sessions, a.offset) if s]
    lt.BLOB_DIR = tempfile.mkdtemp()  # don't fill ~/.headforward while benchmarking
    modes = {"original": None, "headforward": (False, False), "--lossy": (True, False),
             "--smart": (False, True), "--smart --lossy": (True, True)}
    final = {m: 0 for m in modes}
    total = {m: 0 for m in modes}
    per_session = []
    for traj in sessions:
        reqs = to_requests(traj)
        if not reqs:
            continue
        row = {}
        for m, lossy in modes.items():
            sizes = []
            for r in reqs:
                r = copy.deepcopy(r)
                if lossy is not None:
                    lt.compress_request(r, *lossy)
                sizes.append(tok(r))
            final[m] += sizes[-1]
            total[m] += sum(sizes)
            row[m] = sizes[-1]
        per_session.append(row)

    print("%d real SWE-agent sessions, %d requests\n" % (len(per_session), sum(len(to_requests(s)) for s in sessions)))
    print("%-20s %16s %8s %18s %8s" % ("mode", "final context", "saved", "session input", "saved"))
    for m in modes:
        print("%-20s %16s %7.1f%% %18s %7.1f%%" % (
            m, format(final[m], ","), 100.0 * (final["original"] - final[m]) / final["original"],
            format(total[m], ","), 100.0 * (total["original"] - total[m]) / total["original"]))
    print("\nPer-session savings, final context:")
    for m in list(modes)[1:]:
        gains = sorted(100.0 * (r["original"] - r[m]) / r["original"] for r in per_session)
        q = lambda p: gains[min(len(gains) - 1, int(p * len(gains)))]
        print("  %-18s median %5.1f%%   10th pct %5.1f%%   90th pct %5.1f%%" % (m, q(0.5), q(0.1), q(0.9)))


if __name__ == "__main__" and "--verify" not in sys.argv:
    main()


REF = __import__("re").compile(r"^\[(\d+) lines(?: identical to|: same text as) lines (\d+)-(\d+) of (.+?)(?:, numbered (\d+)-(\d+) here)?\]$")


def verify(sessions):
    """Check every reference --smart creates points at exactly the lines it replaced."""
    import tempfile
    lt.BLOB_DIR = tempfile.mkdtemp()
    refs = bad = 0
    for traj in sessions:
        reqs = to_requests(traj)
        if not reqs:
            continue
        orig = reqs[-1]
        comp = copy.deepcopy(orig)
        lt.compress_request(comp, smart=True)
        sent = {}       # label -> lines as sent
        wrote = {}
        n = 0
        o_res = [e for e in lt._walk(orig) if e[0] == "result"]
        c_events = list(lt._walk(comp))
        oi = 0
        for e in c_events:
            if e[0] == "wrote":
                wrote[e[1]] = e[2].split("\n")
                continue
            _, holder, key, cid = e
            text = holder[key]
            truth = lt.compress_tool(o_res[oi][1][o_res[oi][2]])
            oi += 1
            label = "tool call %s" % cid
            lines = text.split("\n")
            rebuilt = []
            for line in lines:
                m = REF.match(line)
                if not m:
                    rebuilt.append(line)
                    continue
                refs += 1
                cnt, a, b, src = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
                pool = sent.get(src) or wrote.get(src) or []
                rebuilt += [lt._key(x) for x in pool[a - 1:b]]
            sent[label] = lines
            want = [lt._key(x) for x in truth.split("\n")]
            got = [lt._key(x) for x in rebuilt]
            if "omitted" not in text and "[Same output" not in text and got != want:
                bad += 1
    print("verified %d references across %d sessions: %d outputs failed to rebuild" % (refs, len(sessions), bad))
    return bad


if __name__ == "__main__" and "--verify" in sys.argv:
    sys.exit(1 if verify([s for s in fetch(200, 0) if s]) else 0)
