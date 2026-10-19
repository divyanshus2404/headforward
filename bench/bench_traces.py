"""Benchmark leantokens on real coding-agent sessions.

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
import leantokens as lt  # noqa: E402

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
    lt.BLOB_DIR = tempfile.mkdtemp()  # don't fill ~/.leantokens while benchmarking
    modes = {"original": None, "leantokens": (False, False), "--lossy": (True, False),
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


if __name__ == "__main__":
    main()
